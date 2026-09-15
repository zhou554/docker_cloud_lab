# docker_cloud_lab

同一仓库内的云原生实验项目：用 Docker Compose 与 Kubernetes 两种方式部署 **FastAPI 短链服务 + MySQL + Redis**。

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

- **第 2–7 节**：Docker / Compose（架构、Dockerfile、通信、Volume、启动、故障）
- **第 8 节**：Kubernetes 部署（本机 Docker Desktop）
- **第 9 节**：Kubernetes 部署（阿里云 k3s 多节点）
- 业务实现细节以 `app/main.py` 为准。

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

---

## 8. Kubernetes 部署（Docker Desktop）

同一业务栈可用 `k8s/` 目录下的清单部署到 **Docker Desktop 自带 Kubernetes**。Compose 文件保留，两种部署方式并存。

### 8.1 Compose ↔ Kubernetes 对照

| Compose | Kubernetes |
|---------|------------|
| 服务名 `mysql` / `redis` / `api` | 同名 Service（集群内 DNS） |
| `.env` 环境变量 | ConfigMap + Secret |
| `mysql_data` / `redis_data` | PVC `mysql-data` / `redis-data` |
| `healthcheck` + `depends_on` | readiness / liveness Probe；api 仍有 lifespan 重试 |
| `8000:8000` | api Service 类型 `NodePort`，集群端口 8000，固定 `nodePort: 30080` |

Namespace：`cloudlab`。

### 8.2 目录说明

```text
k8s/
  namespace.yaml
  configmap.yaml              # 本机 BASE_URL=localhost:30080
  configmap.cloud.example.yaml # 云上 BASE_URL 模板（复制为 configmap.cloud.yaml）
  secret.yaml.example         # 复制为 secret.yaml 后填真实密码
  mysql-pvc.yaml
  mysql-deployment.yaml
  mysql-service.yaml
  redis-pvc.yaml
  redis-deployment.yaml
  redis-service.yaml
  api-deployment.yaml
  api-service.yaml
  kustomization.yaml      # kubectl apply -k k8s/（不含 Secret）
```

### 8.3 前置条件

1. Docker Desktop → Settings → Kubernetes → **Enable Kubernetes**，等待就绪。
2. 确认集群可用：

```powershell
kubectl cluster-info
kubectl get nodes
```

3. 构建 api 本地镜像（Desktop 与 Docker 引擎共享镜像，`imagePullPolicy: IfNotPresent`）：

```powershell
docker build -t docker_cloud_lab:1.0 ./app
```

### 8.4 准备 Secret（勿提交）

```powershell
copy k8s\secret.yaml.example k8s\secret.yaml
# 编辑 k8s\secret.yaml，将 DB_PASSWORD 改为实际密码
```

`k8s/secret.yaml` 已在 `.gitignore` 中，不要推送到 Git。

也可不用文件，直接创建（仍需 **先** 创建 Namespace）：

```powershell
kubectl apply -f k8s/namespace.yaml
kubectl -n cloudlab create secret generic cloudlab-secret --from-literal=DB_PASSWORD=change_me
```

### 8.5 部署

```powershell
kubectl apply -f k8s/namespace.yaml
kubectl apply -f k8s/secret.yaml
kubectl apply -k k8s/
```

若尚未创建 `secret.yaml`，不要跳过 Secret 步骤，否则 MySQL / api Pod 会因缺少 `cloudlab-secret` 起不来。

### 8.6 验证

```powershell
kubectl -n cloudlab get pods,svc,pvc
kubectl -n cloudlab logs -l app=api --tail=50
curl.exe http://localhost:30080/health
```

浏览器：http://localhost:30080/ 、http://localhost:30080/docs  

清单固定 **NodePort 30080**（与 Compose 的 8000 不冲突）。若 PVC `Pending` 且集群无 `local-path` 存储类（部分 Desktop 环境），可暂时去掉 `mysql-pvc.yaml` / `redis-pvc.yaml` 中的 `storageClassName: local-path` 行后重试。

### 8.7 删除

```powershell
kubectl delete -k k8s/
kubectl delete -f k8s/secret.yaml
# 若需一并删除 PVC 数据（会清空 MySQL/Redis 持久化）：
kubectl -n cloudlab delete pvc --all
kubectl delete namespace cloudlab
```

### 8.8 Kubernetes 常见故障

| 现象 | 可能原因 | 处理建议 |
|------|----------|----------|
| api `ImagePullBackOff` / `ErrImageNeverPull` | 本地没有 `docker_cloud_lab:1.0` | 执行 `docker build -t docker_cloud_lab:1.0 ./app`；确认 `imagePullPolicy: IfNotPresent` |
| Pod `CreateContainerConfigError` | 未创建 Secret | `kubectl apply -f k8s/secret.yaml` 或 `create secret` |
| PVC 一直 Pending | 存储类/集群未就绪 | `kubectl get storageclass`；确认 Desktop Kubernetes 已 Running |
| `/health` 503 或 api 未 Ready | MySQL/Redis 未就绪或密码不一致 | `kubectl -n cloudlab get pods`；`logs -l app=mysql`；核对 Secret 与首次初始化密码 |
| 本机 30080 访问失败 | NodePort 未就绪或防火墙 | `kubectl -n cloudlab get svc api` 确认 `8000:30080/TCP`；节点防火墙放行 30080 |
| 改 Secret 后仍用旧密码连库 | PVC 内 MySQL 已按旧密码初始化 | 开发环境可删 PVC/Namespace 后重建（会丢数据） |

查看资源与事件：

```powershell
kubectl -n cloudlab describe pod -l app=api
kubectl -n cloudlab get events --sort-by='.lastTimestamp'
```

---

## 9. Kubernetes 部署（阿里云 k3s 多节点）

清单默认按 **k3s**（`local-path` 存储类、api **NodePort 30080**）编写。镜像仓库（ACR）与 worker 内网拉镜像见下文「镜像」；可先用手工导入镜像完成首次部署。

### 9.1 前置条件

- 1 台 master（建议有公网 SSH）+ 若干 worker，同一 VPC；`kubectl get nodes` 全部 **Ready**。
- 在 **master** 上使用 kubeconfig：

```bash
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
git clone https://github.com/zhou554/docker_cloud_lab.git
cd docker_cloud_lab
```

### 9.2 Secret 与清单

```bash
kubectl apply -f k8s/namespace.yaml
cp k8s/secret.yaml.example k8s/secret.yaml
# 编辑 k8s/secret.yaml，设置 DB_PASSWORD（首次初始化 MySQL 后勿随意改 Secret）
kubectl apply -f k8s/secret.yaml
kubectl apply -k k8s/
kubectl -n cloudlab get pods,svc,pvc -w
```

### 9.3 BASE_URL（短链对外地址）

ConfigMap 默认适合本机 `localhost:30080`。云上需改为 `http://<master公网IP>:30080`（当前为 HTTP；上 HTTPS/Ingress 后再改 URL）。

任选其一：

```bash
# 方式 A：patch（推荐，无需额外文件）
kubectl -n cloudlab patch configmap cloudlab-config --type merge \
  -p '{"data":{"BASE_URL":"http://118.31.68.235:30080"}}'
kubectl -n cloudlab rollout restart deployment/api

# 方式 B：复制 configmap.cloud.example.yaml → configmap.cloud.yaml，改 PUBLIC_IP 后 apply + restart api
```

### 9.4 外网访问与安全组

- Service 已固定 **nodePort: 30080**；在 **master 安全组** 入方向放行 **TCP 30080**（来源建议为你的公网 IP/32）。
- 验证：`curl http://<master公网IP>:30080/health`

### 9.5 镜像（暂未接 ACR 时）

清单中 api 仍为 `docker_cloud_lab:1.0`、`imagePullPolicy: IfNotPresent`。k3s 使用 containerd，需保证 **调度到该 Pod 的节点** 上已有镜像，例如：

- 在 master `docker build` 后 `docker save`，各节点 `k3s ctr images import`；或
- 推送 ACR 后 `kubectl set image deployment/api -n cloudlab api=<ACR 完整地址>`（worker 需能 pull，常配合 VPC 内网访问 ACR 或 NAT）。

MySQL / Redis 使用公共镜像名，worker 无公网时需同样解决出网或预拉取。

### 9.6 多节点与释放后重建

- `kubectl -n cloudlab scale deployment api --replicas=3` 后 `get pods -o wide` 可查看跨节点调度。
- **释放 ECS 再开新机器**：nodePort **30080** 不变（清单写死）；**公网 IP 与 BASE_URL 需重新 patch**；Secret、PVC 数据需重新部署（除非另行保留云盘）。

### 9.7 常见故障（云上补充）

| 现象 | 可能原因 | 处理建议 |
|------|----------|----------|
| `namespaces "cloudlab" not found` | 先 apply Secret | 先 `kubectl apply -f k8s/namespace.yaml` |
| api `ImagePullBackOff` on worker | 节点无镜像 / 无出网 | 各节点 import 或 ACR + 内网 pull |
| 公网 curl 超时 | 安全组未放行 30080 | 阿里云安全组入方向添加规则 |
| 短链仍是 localhost | BASE_URL 未改或未重启 api | patch ConfigMap + `rollout restart deployment/api` |
