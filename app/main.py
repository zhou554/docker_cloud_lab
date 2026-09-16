import os
import re
import time
import logging
import json
import hashlib
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Depends
from fastapi.responses import RedirectResponse, JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, Gauge, generate_latest
from pydantic import BaseModel, HttpUrl, field_validator
from sqlalchemy import create_engine, Column, Integer, String, DateTime, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import declarative_base, sessionmaker, Session
import redis

# 日志配置
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# 配置中心：全部环境变量化
class Settings:
    APP_NAME = os.getenv("APP_NAME", "docker-cloud-lab")
    # 生成短链完整 URL 用的对外地址，K8s 时映射为 Ingress 域名
    BASE_URL = os.getenv("BASE_URL", "http://localhost:8000")
    DEBUG = os.getenv("DEBUG", "false").lower() == "true"
    # MySQL
    DB_HOST = os.getenv("DB_HOST", "mysql")
    DB_PORT = int(os.getenv("DB_PORT", "3306"))
    DB_USER = os.getenv("DB_USER", "root")
    DB_PASSWORD = os.getenv("DB_PASSWORD", "password")
    DB_NAME = os.getenv("DB_NAME", "cloudlab")
    # Redis
    REDIS_HOST = os.getenv("REDIS_HOST", "redis")
    REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
    REDIS_DB = int(os.getenv("REDIS_DB", "0"))

settings = Settings()

# 数据库 & Redis 连接
DATABASE_URL = (
    f"mysql+pymysql://{settings.DB_USER}:{settings.DB_PASSWORD}"
    f"@{settings.DB_HOST}:{settings.DB_PORT}/{settings.DB_NAME}"
)
engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_recycle=3600,
    pool_size=10,
    max_overflow=20,
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

redis_client = redis.Redis(
    host=settings.REDIS_HOST,
    port=settings.REDIS_PORT,
    db=settings.REDIS_DB,
    decode_responses=True,
    socket_connect_timeout=5,
    socket_timeout=5,
    health_check_interval=30,
)

# 数据模型
class Link(Base):
    __tablename__ = "links"
    id = Column(Integer, primary_key=True, index=True)
    original_url = Column(String(2048), nullable=False)
    short_code = Column(String(10), unique=True, index=True, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))

# 保留路径黑名单：这些路由先于 /{short_code} 注册，用它们做短码会永远访问不到
RESERVED_CODES = {
    "health", "metrics", "docs", "redoc",
    "openapi.json", "links", "chaos", "favicon.ico",
}


# Pydantic 模型
class LinkCreate(BaseModel):
    url: HttpUrl
    custom_code: str | None = None

    @field_validator("custom_code")
    @classmethod
    def validate_custom_code(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if not 1 <= len(v) <= 10:
            raise ValueError("custom_code must be 1-10 characters")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", v):
            raise ValueError("custom_code may only contain letters, digits, '-' and '_'")
        if v.lower() in RESERVED_CODES:
            raise ValueError(f"custom_code '{v}' is a reserved path")
        return v


class LinkResponse(BaseModel):
    short_code: str
    original_url: str
    short_url: str
    created_at: datetime

# 依赖注入
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def generate_short_code(url: str, custom: str | None = None) -> str:
    if custom:
        return custom[:10]
    return hashlib.md5(url.encode()).hexdigest()[:6]

# 应用生命周期
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(f"🚀 Starting {settings.APP_NAME}...")
    # MySQL 连接重试（数据库启动可能比应用慢）
    for i in range(15):
        try:
            Base.metadata.create_all(bind=engine)
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            logger.info("✅ MySQL connected.")
            break
        except Exception as e:
            logger.warning(f"⏳ Waiting for MySQL ({i+1}/15): {e}")
            time.sleep(2)
    else:
        logger.error("❌ MySQL connection failed after retries")
        raise Exception("MySQL connection failed")
    # Redis 连接验证
    try:
        redis_client.ping()
        logger.info("✅ Redis connected.")
    except Exception as e:
        logger.error(f"❌ Redis connection failed: {e}")
        raise
    yield
    # 优雅关闭
    logger.info("🛑 Shutting down gracefully...")
    redis_client.close()
    engine.dispose()

app = FastAPI(
    title=settings.APP_NAME,
    version="1.0.0",
    lifespan=lifespan,
)

APP_UP = Gauge("app_up", "1 if overall /health is ok else 0")
MYSQL_UP = Gauge("mysql_up", "1 if MySQL SELECT 1 succeeds else 0")
REDIS_UP = Gauge("redis_up", "1 if Redis PING succeeds else 0")

# 健康检查
@app.get("/health")
def health_check():
    checks = {}
    status_code = 200
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        checks["mysql"] = "ok"
        MYSQL_UP.set(1)
    except Exception as e:
        checks["mysql"] = f"error: {str(e)}"
        status_code = 503
        MYSQL_UP.set(0)
    try:
        redis_client.ping()
        checks["redis"] = "ok"
        REDIS_UP.set(1)
    except Exception as e:
        checks["redis"] = f"error: {str(e)}"
        status_code = 503
        REDIS_UP.set(0)
    APP_UP.set(1 if status_code == 200 else 0)
    return JSONResponse(
        status_code=status_code,
        content={
            "status": "healthy" if status_code == 200 else "unhealthy",
            "checks": checks,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
    )


@app.get("/metrics")
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

# 业务接口
@app.get("/")
def root():
    return {
        "service": settings.APP_NAME,
        "version": "1.0.0",
        "endpoints": {
            "docs": "/docs",
            "health": "/health",
            "metrics": "/metrics",
            "create_link": "POST /links/",
            "redirect": "GET /{short_code}",
            "stats": "GET /links/{short_code}/stats"
        }
    }

def _link_response(link: Link) -> dict:
    """统一构造 LinkResponse 字典"""
    return {
        "short_code": link.short_code,
        "original_url": link.original_url,
        "short_url": f"{settings.BASE_URL}/{link.short_code}",
        "created_at": link.created_at,
    }


@app.post("/links/", response_model=LinkResponse)
def create_link(req: LinkCreate, db: Session = Depends(get_db)):
    """创建短链接 → 写 MySQL + 预热 Redis 缓存"""
    original = str(req.url)

    if req.custom_code:
        # 自定义短码：冲突直接报 400
        short_code = req.custom_code
        existing = db.query(Link).filter(Link.short_code == short_code).first()
        if existing:
            if existing.original_url == original:
                return _link_response(existing)
            raise HTTPException(status_code=400, detail="Custom short code already exists")
        link = Link(original_url=original, short_code=short_code)
        db.add(link)
        try:
            db.commit()
        except IntegrityError:
            # 并发下同一短码被其他请求抢先插入
            db.rollback()
            existing = db.query(Link).filter(Link.short_code == short_code).first()
            if existing and existing.original_url == original:
                return _link_response(existing)
            raise HTTPException(status_code=400, detail="Custom short code already exists")
        db.refresh(link)
    else:
        # 自动生成 6 位短码：MD5 碰撞时加确定性盐重试（保证同一 URL 幂等）
        link = None
        for attempt in range(5):
            candidate = original if attempt == 0 else f"{original}#{attempt}"
            short_code = generate_short_code(candidate)
            existing = db.query(Link).filter(Link.short_code == short_code).first()
            if existing:
                if existing.original_url == original:
                    return _link_response(existing)
                continue  # 与别人的 URL 碰撞，加盐重试
            link = Link(original_url=original, short_code=short_code)
            db.add(link)
            try:
                db.commit()
                break
            except IntegrityError:
                # 并发下同一短码被其他请求抢先插入
                db.rollback()
                existing = db.query(Link).filter(Link.short_code == short_code).first()
                if existing and existing.original_url == original:
                    return _link_response(existing)
                continue
        else:
            raise HTTPException(status_code=500, detail="Failed to generate a unique short code")
        db.refresh(link)

    # 预热缓存（1小时过期）
    redis_client.setex(f"link:{short_code}", 3600, original)
    redis_client.setex(f"meta:{short_code}", 3600, json.dumps({
        "original_url": original,
        "created_at": link.created_at.isoformat()
    }))
    logger.info(f"Link created: {short_code} -> {original}")
    return _link_response(link)

@app.get("/{short_code}")
def redirect_link(short_code: str):
    """访问短链接 → 先读 Redis 缓存，没有再查 MySQL，302 跳转"""
    # 1. 查 Redis
    cached_url = redis_client.get(f"link:{short_code}")
    if cached_url:
        redis_client.hincrby(f"stats:{short_code}", "clicks", 1)
        redis_client.hincrby(f"stats:global", "cache_hits", 1)
        logger.info(f"Cache HIT: {short_code}")
        return RedirectResponse(url=cached_url, status_code=302)
    # 2. 缓存未命中 → 查 MySQL
    db = SessionLocal()
    try:
        link = db.query(Link).filter(Link.short_code == short_code).first()
        if not link:
            raise HTTPException(status_code=404, detail="Short link not found")
        # 回填缓存
        redis_client.setex(f"link:{short_code}", 3600, link.original_url)
        redis_client.hincrby(f"stats:{short_code}", "clicks", 1)
        redis_client.hincrby(f"stats:global", "cache_misses", 1)
        logger.info(f"Cache MISS -> DB: {short_code}")
        return RedirectResponse(url=link.original_url, status_code=302)
    finally:
        db.close()

@app.get("/links/{short_code}/stats")
def get_stats(short_code: str):
    """查看短链接统计（点击数、缓存命中率）"""
    stats = redis_client.hgetall(f"stats:{short_code}")
    global_stats = redis_client.hgetall(f"stats:global")
    meta = redis_client.get(f"meta:{short_code}")
    if meta:
        meta = json.loads(meta)
    else:
        db = SessionLocal()
        try:
            link = db.query(Link).filter(Link.short_code == short_code).first()
            if not link:
                raise HTTPException(status_code=404, detail="Short link not found")
            meta = {
                "original_url": link.original_url,
                "created_at": link.created_at.isoformat()
            }
        finally:
            db.close()
    hits = int(global_stats.get("cache_hits", 0))
    misses = int(global_stats.get("cache_misses", 0))
    total = hits + misses
    hit_rate = round(hits / total * 100, 2) if total > 0 else 0
    return {
        "short_code": short_code,
        "original_url": meta.get("original_url"),
        "created_at": meta.get("created_at"),
        "clicks": int(stats.get("clicks", 0)),
        "cache": {
            "hits": hits,
            "misses": misses,
            "hit_rate_percent": hit_rate
        }
    }