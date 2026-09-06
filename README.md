# docker_cloud_lab

Docker / Compose 实验项目：将 FastAPI 短链服务与 MySQL、Redis 编排为可本地一键启动的多容器栈。

---

## 1. 项目描述

业务与配置均来自仓库内应用代码（`app/main.py`、`app/requirements.txt`）及编排文件。

### 业务是什么

一个 **URL 短链服务**（默认服务名 `docker-cloud-lab`）：

| 能力 | 接口 | 行为（与代码一致） |
|------|------|-------------------|
| 服务信息 | `GET /` | 返回服务名、版本与接口清单 |
| 健康检查 | `GET /health` | 检查 MySQL（`SELECT 1`）与 Redis（`PING`），异常返回 503 |
| 创建短链 | `POST /links/` | 写入 MySQL，并向 Redis 预热缓存（约 1 小时过期） |
| 短链跳转 | `GET /{short_code}` | 先读 Redis，未命中再查 MySQL 并回填缓存，302 跳转 |
| 访问统计 | `GET /links/{short_code}/stats` | 读取 Redis 中的点击与缓存命中等统计 |

配置全部环境变量化（`Settings`）：`DB_*`、`REDIS_*`、`BASE_URL`、`APP_NAME`、`DEBUG` 等，由 Compose / `.env` 注入；`BASE_URL` 用于拼对外短链完整地址。

依赖技术栈见 `app/requirements.txt`：FastAPI、Uvicorn、SQLAlchemy、PyMySQL、Redis、Pydantic 等。

### 本 README 的范围

本文档只覆盖 **Docker 部分**：架构、Dockerfile、容器通信、Volume、启动方式与常见故障。业务实现细节以 `app/main.py` 为准。

---

## 2. 项目架构

```text
                    宿主机
         http://localhost:8000
                      │
                      ▼
         ┌────────────────────────┐
         │ api  (cloudlab_api)     │
         │ 镜像: build ./app      │
         │ 监听: 0.0.0.0:8000     │
         └───────────┬────────────┘
                     │
         网络: cloudlab_net (bridge)
          ┌──────────┴──────────┐
          ▼                     ▼
 ┌─────────────────┐   ┌─────────────────┐
 │ mysql:8.0       │   │ redis:7-alpine  │
 │ 容器名          │   │ 容器名          │
 │ cloudlab_mysql  │   │ cloudlab_redis  │
 │ 端口 3306       │   │ 端口 6379       │
 │ 卷 mysql_data   │   │ 卷 redis_data   │
 └─────────────────┘   └─────────────────┘
```

| 服务 | 镜像来源 | 职责 |
|------|----------|------|
| **api** | `build: ./app`（本仓库 Dockerfile） | 对外提供 HTTP API |
| **mysql** | `mysql:8.0` | 持久保存短链记录 |
| **redis** | `redis:7-alpine` | 缓存与统计；开启 AOF |

**端口设计：**

| 服务 | 容器端口 | 宿主机映射 | 说明 |
|------|----------|------------|------|
| api | 8000 | `8000:8000` | 唯一对外入口 |
| mysql | 3306 | 不映射 | 仅集群内网访问 |
| redis | 6379 | 不映射 | 仅集群内网访问 |

启动顺序：mysql / redis 先通过 `healthcheck` 变为 healthy，api 再启动（`depends_on` + `condition: service_healthy`）。

---

## 3. Dockerfile 说明

路径：`app/dockerfile`。构建上下文为 `./app`（与 Compose 中 `build: ./app` 一致）。

### 多阶段构建

**阶段一 `builder`**

- 基础镜像：`python:3.11-slim`
- `WORKDIR /build`，只拷贝 `requirements.txt`
- `pip install --user` 将依赖装到 `/root/.local`（使用清华 PyPI 镜像加速）
- 目的：把安装过程留在构建阶段，减小最终运行镜像干扰面，并利用层缓存（依赖不变时不必重装）

**阶段二 runtime**

- 再次 `FROM python:3.11-slim`
- 创建系统组/用户 `appgroup` / `appuser`（`-m` 创建家目录）
- `WORKDIR /app`：放置业务代码（`main.py` 等）
- 从 builder 拷贝 `/root/.local` → `/home/appuser/.local`，并设置 `PATH`、`HOME`
- `COPY` 应用代码后 `USER appuser`，以非 root 运行
- `EXPOSE 8000`；`CMD` 启动：`uvicorn main:app --host 0.0.0.0 --port 8000`

### HEALTHCHECK

容器内定期请求：

`http://localhost:8000/health`

与应用 `/health` 对齐；依赖异常时探针失败。`start-period=40s` 给启动与依赖连接留出时间。

### `.dockerignore`

`app/.dockerignore` 排除 `__pycache__`、`.env`、`.venv`、日志等，减小上下文并避免把密钥打进镜像。

---

## 4. 容器之间如何通信

1. **同一自定义网络**  
   三个服务都加入 `cloudlab_net`（`driver: bridge`）。只有在同一网络内，才能用 Compose **服务名**互相解析。

2. **DNS：服务名 = 主机名**  
   - api 通过环境变量 `DB_HOST=mysql` 连接 `mysql:3306`  
   - api 通过 `REDIS_HOST=redis` 连接 `redis:6379`  
   这里的 `mysql` / `redis` 是 `docker_compose.yaml` 里的 **服务名**，不是容器名（容器名是 `cloudlab_mysql` 等，一般不用来做应用配置）。

3. **对外与对内分离**  
   - 浏览器只访问宿主机 `localhost:8000` → 映射进 api 容器  
   - MySQL / Redis **不**映射到宿主机，外网不能直连；仅 api 在 `cloudlab_net` 内访问

4. **配置传递**  
   根目录 `.env`（模板见 `.env.example`）由 Compose 读取；api 使用 `env_file` + `environment`，mysql 使用 `${DB_PASSWORD}` / `${DB_NAME}`，保证应用与数据库密码、库名一致。

5. **业务数据路径（通信之上的协作）**  
   - 写：api → MySQL（落库）+ Redis（缓存）  
   - 读：api → Redis（优先）→ 未命中再 → MySQL  

---

## 5. Volume 如何持久化

在 `docker_compose.yaml` 中声明了两个 **named volume**：

| Volume 名 | 挂载到容器内路径 | 作用 |
|-----------|------------------|------|
| `mysql_data` | `/var/lib/mysql` | MySQL 数据文件；容器删除后短链数据仍在 |
| `redis_data` | `/data` | Redis 数据目录；配合 `redis-server --appendonly yes`（AOF）持久化缓存与统计相关数据 |

特点：

- 卷由 Compose 项目自动创建（名大致为 `docker_cloud_lab_mysql_data` 等）
- `docker compose down`：**默认不删卷**，数据保留  
- `docker compose down -v`：**删除卷**，MySQL/Redis 数据清空  
- 注意：MySQL 首次初始化后 root 密码写在数据目录里；之后只改 `.env` 里的密码，**不会**自动改已有卷中的密码

---

## 6. 如何启动

### 环境要求

- 已安装 Docker Desktop（或 Docker Engine + Compose V2）

### 配置环境变量

```powershell
cd D:\AAA_cursor_P\docker_cloud_lab
copy .env.example .env
```

按需修改 `.env` 中的 `DB_PASSWORD` 等（勿将 `.env` 提交到 Git）。

### 构建并后台启动

```powershell
docker compose -f docker_compose.yaml up -d --build
```

### 验证

```powershell
docker compose -f docker_compose.yaml ps
curl http://localhost:8000/health
```

浏览器可访问：

- http://localhost:8000/
- http://localhost:8000/health
- http://localhost:8000/docs

### 仅构建 api 镜像（可选）

```powershell
docker build -t docker_cloud_lab:1.0 ./app
```

完整功能仍需 Compose 同时提供 MySQL 与 Redis。

### 停止

```powershell
docker compose -f docker_compose.yaml down      # 保留卷
docker compose -f docker_compose.yaml down -v   # 同时删卷（慎用）
```

---

## 7. 常见故障

| 现象 | 可能原因 | 处理建议 |
|------|----------|----------|
| 拉 `mysql`/`redis` 卡在 Pulling | Docker Hub 网络慢或不稳 | 配置 Docker `registry-mirrors` 后重试；或显式从镜像站 pull 再 `docker tag` 为官方名 |
| api 一直 Restarting / 起不来 | MySQL/Redis 未就绪，或密码不一致 | `docker compose logs api`；确认 `.env` 与 mysql 环境变量一致；依赖 health 通过后再看 api 日志 |
| `/health` 返回 503 | MySQL 或 Redis 检查失败 | 看返回体里 `checks.mysql` / `checks.redis`；`logs mysql` / `logs redis` |
| 改了 `.env` 密码仍连不上库 | 旧 `mysql_data` 卷已按旧密码初始化 | 开发环境可用 `down -v` 清空后重建（会丢数据） |
| `version` is obsolete 警告 | Compose 新版本忽略 `version` 字段 | 可删除文件首行 `version:`，不影响运行 |
| 构建卡在 `pip install` | 容器访问 PyPI 慢 | Dockerfile 已使用清华源；仍慢则检查本机/Docker 网络 |
| 端口被占用 | 宿主机 8000 已被占用 | 改 compose 中 `"8000:8000"` 左侧端口，或释放占用进程 |
| 找不到 `.env` / 变量为空 | 未复制 `.env.example` | 在项目根目录创建 `.env` 后再 `up` |

查看日志：

```powershell
docker compose -f docker_compose.yaml logs -f api
docker compose -f docker_compose.yaml logs -f mysql
docker compose -f docker_compose.yaml logs -f redis
```
