# docker_cloud_lab

同一仓库内的云原生实验项目：用 Docker Compose 与 Kubernetes 部署 FastAPI 短链服务 + MySQL + Redis。

国内网络环境：本机仅用 Compose 联调（第 6–7 节）；K8s 业务栈、CI/CD、Prometheus/Grafana 告警均在阿里云 k3s 完成（第 9–10 节）。**不做本机 K8s 验证**（第 8 节 minikube / WSL 可整节跳过），直接在多节点 k3s 上 apply 同一套 k8s/、monitoring/ 清单。

## 1. 项目描述

业务与配置均来自仓库内应用代码（`app/main.py`、`app/requirements.txt`）及编排文件。

### 业务是什么

一个 URL 短链服务（默认服务名 `docker-cloud-lab`）：

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

- 第 2–7 节：Docker / Compose（本机联调）
- 第 8 节：Compose 与 K8s 概念对照（无本机 K8s 步骤）
- 第 9 节：**主路径** — 阿里云 k3s 多节点部署与 CI/CD
- 第 10 节：**主路径** — 云上 Prometheus / Grafana / 告警与截图
- 第 11 节：仓库目录与脚本索引
- 第 12 节：附录 — 可选本机 minikube（默认跳过）
- 脚本：`tools/cloud/*.sh`；可选 `tools/optional-local-minikube/`
- 业务实现细节以 `app/main.py` 为准。

### 本机 vs 云上

| 内容 | 本机 | 云上（k3s） |
|------|------|-------------|
| 改代码、测 API | Compose（第 6 节） | 可选 |
| K8s 部署 | 可不做（无本机验证要求） | **必做**（第 9 节多节点） |
| 监控与告警截图 | 不做 | **必做**（第 10 节） |
| CI/CD | 不做 | **必做**（接 ACR） |

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
| api | `build: ./app`（本仓库 Dockerfile） | 对外提供 HTTP API |
| mysql | `mysql:8.0` | 持久保存短链记录 |
| redis | `redis:7-alpine` | 缓存与统计；开启 AOF |

端口设计：

| 服务 | 容器端口 | 宿主机映射 | 说明 |
|------|----------|------------|------|
| api | 8000 | `8000:8000` | 唯一对外入口 |
| mysql | 3306 | 不映射 | 仅集群内网访问 |
| redis | 6379 | 不映射 | 仅集群内网访问 |

启动顺序：mysql / redis 先通过 `healthcheck` 变为 healthy，api 再启动（`depends_on` + `condition: service_healthy`）。

## 3. Dockerfile 说明

路径：`app/dockerfile`。构建上下文为 `./app`（与 Compose 中 `build: ./app` 一致）。

### 多阶段构建

阶段一 `builder`

- 基础镜像：`python:3.11-slim`
- `WORKDIR /build`，只拷贝 `requirements.txt`
- `pip install --user` 将依赖装到 `/root/.local`（使用清华 PyPI 镜像加速）
- 目的：把安装过程留在构建阶段，减小最终运行镜像干扰面，并利用层缓存（依赖不变时不必重装）

阶段二 runtime

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

## 4. 容器之间如何通信

1. 同一自定义网络  
   三个服务都加入 `cloudlab_net`（`driver: bridge`）。只有在同一网络内，才能用 Compose 服务名互相解析。

2. DNS：服务名 = 主机名  
   - api 通过环境变量 `DB_HOST=mysql` 连接 `mysql:3306`  
   - api 通过 `REDIS_HOST=redis` 连接 `redis:6379`  
   这里的 `mysql` / `redis` 是 `docker_compose.yaml` 里的 服务名，不是容器名（容器名是 `cloudlab_mysql` 等，一般不用来做应用配置）。

3. 对外与对内分离  
   - 浏览器只访问宿主机 `localhost:8000` → 映射进 api 容器  
   - MySQL / Redis 不映射到宿主机，外网不能直连；仅 api 在 `cloudlab_net` 内访问

4. 配置传递  
   根目录 `.env`（模板见 `.env.example`）由 Compose 读取；api 使用 `env_file` + `environment`，mysql 使用 `${DB_PASSWORD}` / `${DB_NAME}`，保证应用与数据库密码、库名一致。

5. 业务数据路径（通信之上的协作）  
   - 写：api → MySQL（落库）+ Redis（缓存）  
   - 读：api → Redis（优先）→ 未命中再 → MySQL  

## 5. Volume 如何持久化

在 `docker_compose.yaml` 中声明了两个 named volume：

| Volume 名 | 挂载到容器内路径 | 作用 |
|-----------|------------------|------|
| `mysql_data` | `/var/lib/mysql` | MySQL 数据文件；容器删除后短链数据仍在 |
| `redis_data` | `/data` | Redis 数据目录；配合 `redis-server --appendonly yes`（AOF）持久化缓存与统计相关数据 |

特点：

- 卷由 Compose 项目自动创建（名大致为 `docker_cloud_lab_mysql_data` 等）
- `docker compose down`：默认不删卷，数据保留  
- `docker compose down -v`：删除卷，MySQL/Redis 数据清空  
- 注意：MySQL 首次初始化后 root 密码写在数据目录里；之后只改 `.env` 里的密码，不会自动改已有卷中的密码

## 6. 如何启动

### 环境要求

- 已安装 Docker Desktop（或 Docker Engine + Compose V2）

### 配置环境变量

```powershell
cd D:\AAA_cursor_P\docker_cloud_lab
copy .env.example .env
```

按需修改 `.env` 中的 `DB_PASSWORD` 等（**勿将 `.env` 提交到 Git**）。

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
$env:DOCKER_BUILDKIT = "0"
docker build -t docker_cloud_lab:1.0 -f app/dockerfile ./app
```

完整功能仍需 Compose 同时提供 MySQL 与 Redis。

### 停止

```powershell
docker compose -f docker_compose.yaml down      # 保留卷
docker compose -f docker_compose.yaml down -v   # 同时删卷（慎用）
```

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

## 8. Compose 与 Kubernetes 对照

K8s、监控、CI/CD 均在云上完成（第 9–10 节）。本机不必安装 minikube；可选练习见第 12 节。

### 8.1 对照表

| Compose | Kubernetes |
|---------|------------|
| 服务名 `mysql` / `redis` / `api` | 同名 Service（集群内 DNS） |
| `.env` 环境变量 | ConfigMap + Secret |
| `mysql_data` / `redis_data` | PVC `mysql-data` / `redis-data` |
| `healthcheck` + `depends_on` | readiness / liveness Probe；api 仍有 lifespan 重试 |
| `8000:8000` | api Service 类型 `NodePort`，集群端口 8000，固定 `nodePort: 30080` |

Namespace：`cloudlab`。

### 8.2 `k8s/` 清单与 apply 顺序

在 master 上（`export KUBECONFIG=/etc/rancher/k3s/k3s.yaml`）：

1. `kubectl apply -f k8s/namespace.yaml`
2. `cp k8s/secret.yaml.example k8s/secret.yaml` 并编辑 → `kubectl apply -f k8s/secret.yaml`
3. `kubectl apply -k k8s/`（不含 Secret；也可 `bash tools/cloud/deploy-business.sh`）
4. 节点 ACR 与 `acr-pull` Secret，见 9.5
5. `PUBLIC_IP=<公网IP> bash tools/cloud/patch-base-url.sh`

```text
k8s/
  namespace.yaml
  configmap.yaml                 # BASE_URL 占位，部署后必须 patch
  configmap.cloud.example.yaml   # 云上 BASE_URL 整文件替换模板
  secret.yaml.example            # → secret.yaml（已 .gitignore）
  acr-pull-secret.example.yaml   # 可选：ACR imagePullSecrets
  mysql-pvc.yaml / mysql-deployment.yaml / mysql-service.yaml
  redis-pvc.yaml / redis-deployment.yaml / redis-service.yaml
  api-deployment.yaml / api-service.yaml
  kustomization.yaml
monitoring/                      # Prometheus + Grafana（第 10 节）
tools/cloud/                     # 部署脚本
```

## 9. Kubernetes 部署（阿里云 k3s 多节点）

本章为 **K8s 主路径**。本机仅 Compose 联调，不做本机 K8s 验证。

### 9.0 云上一条龙

| 步骤 | 操作 |
|------|------|
| 1 | k3s 多节点 Ready，克隆仓库，`export KUBECONFIG=/etc/rancher/k3s/k3s.yaml` |
| 2 | 准备 `k8s/secret.yaml` → `bash tools/cloud/deploy-business.sh` |
| 3 | 节点 ACR（pause + registries）+ `acr-pull` Secret；清单已含 ACR 镜像（9.5） |
| 4 | `PUBLIC_IP=x.x.x.x bash tools/cloud/patch-base-url.sh`；安全组 30080 |
| 5 | `bash tools/cloud/deploy-monitoring.sh`；安全组 30090、30300 |
| 6 | 截图存 `docs/screenshots/`（10.3）；CI 见 9.5 |

开发循环：Compose 改代码 → Git push → CI 推 ACR → master 上 `kubectl set image` 更新 api tag（见 9.5）。

### 9.1 前置条件与安全组

- 1 台 master（建议公网 SSH）+ 若干 worker，同一 VPC；`kubectl get nodes` 全部 Ready。

```bash
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
git clone https://github.com/zhou554/docker_cloud_lab.git
cd docker_cloud_lab
```

安全组入方向（来源建议为你的公网 IP/32）：

| 端口 | 用途 |
|------|------|
| 22 | SSH |
| 30080 | api NodePort |
| 30090 | Prometheus（监控部署后） |
| 30300 | Grafana（监控部署后） |

### 9.2 Secret 与业务栈

```bash
cp k8s/secret.yaml.example k8s/secret.yaml
# 编辑 DB_PASSWORD（MySQL 首次初始化后勿随意改 Secret，除非重建 PVC）

bash tools/cloud/deploy-business.sh
```

等价手工命令：

```bash
kubectl apply -f k8s/namespace.yaml
kubectl apply -f k8s/secret.yaml
kubectl apply -k k8s/
kubectl -n cloudlab get pods,svc,pvc -w
```

### 9.3 BASE_URL（短链对外地址）

`BASE_URL` 部署后必须改为公网地址：

```bash
PUBLIC_IP=<master公网IP> bash tools/cloud/patch-base-url.sh
```

或复制 `configmap.cloud.example.yaml` → `configmap.cloud.yaml` 后 apply + restart api。

### 9.4 外网访问与安全组

- Service 已固定 nodePort: 30080；在 master 安全组 入方向放行 TCP 30080（来源建议为你的公网 IP/32）。
- 验证：`curl http://<master公网IP>:30080/health`

### 9.5 镜像与 CI/CD

国内 ECS 通常 **无法稳定访问 Docker Hub**。本仓库 `k8s/`、`monitoring/` 中业务与监控镜像已指向 **个人版 ACR（VPC 域名）**，命名空间 **`zhou554_cloudlab`**；K8s 资源仍在命名空间 **`cloudlab` / `monitoring`**（二者不同，正常）。

| 用途 | 域名示例 |
|------|----------|
| 本机 / GitHub CI **push** | `crpi-6zjjswgvnui3es9q.cn-hangzhou.personal.cr.aliyuncs.com` |
| 集群 **pull**（与清单 `image:` 一致） | `crpi-6zjjswgvnui3es9q-vpc.cn-hangzhou.personal.cr.aliyuncs.com` |

**节点（每台 master + worker，一次性）**

1. `/etc/rancher/k3s/registries.yaml`：VPC 域名 + ACR 账号密码（见控制台访问凭证）。
2. `/etc/rancher/k3s/config.yaml`：`pause-image: <VPC域名>/zhou554_cloudlab/pause:3.10.2`
3. 重启 `k3s`（worker 为 `k3s-agent`）。

**集群 Secret（master 上）**

`k8s/acr-pull-secret.example.yaml` 中有命令：在 **`cloudlab`** 与 **`monitoring`** 各创建 `acr-pull`，`--docker-server` 必须为 **VPC 域名**（与 Deployment 中 image 主机名一致）。

**部署**

```bash
kubectl apply -f k8s/namespace.yaml
kubectl apply -f k8s/secret.yaml
kubectl apply -k k8s/
kubectl apply -k monitoring/
```

**GitHub Actions（CI，仅 api）**

复制 `.github/workflows/build-push-acr.yml.example` 为 `build-push-acr.yml`，在 Settings → Secrets 配置：

| Secret | 示例值 |
|--------|--------|
| `ACR_REGISTRY` | `crpi-6zjjswgvnui3es9q.cn-hangzhou.personal.cr.aliyuncs.com` |
| `ACR_NAMESPACE` | `zhou554_cloudlab` |
| `ACR_USERNAME` / `ACR_PASSWORD` | ACR 登录凭证 |

推送 `app/` 变更后 CI 构建并 push `.../zhou554_cloudlab/api:<git-sha>`。

**发版更新 api（CD 可自动化此步）**

```bash
REG_VPC="crpi-6zjjswgvnui3es9q-vpc.cn-hangzhou.personal.cr.aliyuncs.com"
kubectl -n cloudlab set image deployment/api \
  api=${REG_VPC}/zhou554_cloudlab/api:<与 CI 相同的 git-sha>
kubectl -n cloudlab rollout status deployment/api
```

或修改 `k8s/api-deployment.yaml` 中 api 的 tag → `git pull` → `kubectl apply -k k8s/`。

mysql / redis / 监控镜像 tag 固定，已在清单中指向 ACR；**仅 api** 随 CI 频繁变更 tag。

**无 ACR 时的备选（不推荐）**

各节点 `k3s ctr images import` 离线包；见 `tools/cloud/` 下 build/import 脚本（若存在）。

### 9.6 多节点与释放后重建

- `kubectl -n cloudlab scale deployment api --replicas=3` 后 `get pods -o wide` 可查看跨节点调度。
- 释放 ECS 再开新机器：nodePort 30080 不变（清单写死）；公网 IP 与 BASE_URL 需重新 patch；Secret、PVC 数据需重新部署（除非另行保留云盘）。

### 9.7 常见故障（云上补充）

| 现象 | 可能原因 | 处理建议 |
|------|----------|----------|
| `namespaces "cloudlab" not found` | 先 apply Secret | 先 `kubectl apply -f k8s/namespace.yaml` |
| api `ImagePullBackOff` on worker | 节点无镜像 / 无出网 | ACR VPC pull + `acr-pull`；或各节点 import |
| Pod 卡在 sandbox / 拉 `docker.io` pause | 未配节点 `pause-image` | 每台节点 ACR pause + `registries.yaml` 后重启 k3s |
| 401 pull denied | Secret 与 image 域名不一致 | `acr-pull` 的 `--docker-server` 用 VPC 域名 |
| 公网 curl 超时 | 安全组未放行 30080 | 阿里云安全组入方向添加规则 |
| 短链仍是 localhost | BASE_URL 未改或未重启 api | patch ConfigMap + `rollout restart deployment/api` |
| Pod `CreateContainerConfigError` | 缺少 Secret | 先 apply `k8s/secret.yaml` |
| `/health` 503 | MySQL/Redis 未 Ready 或密码不一致 | 查 Pod 日志；Secret 与 PVC 首次初始化密码 |
| 改 Secret 后仍连不上库 | PVC 已按旧密码初始化 | 删 PVC/Namespace 后重建（丢数据） |

### 9.8 卸载（云上）

```bash
kubectl delete -k monitoring/
kubectl delete -k k8s/
kubectl delete -f k8s/secret.yaml
# 清空持久化数据（慎用）：
kubectl -n cloudlab delete pvc --all
kubectl delete namespace cloudlab
kubectl delete namespace monitoring
```

## 10. 监控（Prometheus + Grafana）

仅在云上 k3s 部署（第 9 节业务栈已 Running）。节点需能拉取或已 import：`prom/prometheus`、`grafana/grafana`、`prom/node-exporter`。

### 10.1 组件

| 组件 | 作用 | 云上入口（master 公网 IP） |
|------|------|---------------------------|
| Prometheus | 抓取 `/metrics`、评估告警 | `http://<公网IP>:30090` |
| node-exporter | Node CPU / 内存 / 磁盘 | 仅集群内 |
| Grafana | 看板 | `http://<公网IP>:30300`（`admin` / `admin`，仅实验环境） |

短链 api 增加 `GET /metrics`（`app_up` / `mysql_up` / `redis_up`）。Pod 注解 `prometheus.io/scrape=true`。

告警（Prometheus → Alerts）：

- `CloudlabApiDown`：`up{job="cloudlab-api"} == 0` 持续 1 分钟
- `CloudlabAppUnhealthy`：`app_up == 0` 持续 1 分钟（`/health` 失败，对应探针失败）
- `CloudlabMysqlDown` / `CloudlabRedisDown`

### 10.2 部署（云上）

前提：第 9 节 `cloudlab` 命名空间内 api / mysql / redis 已 Running，且 api 镜像含 `GET /metrics`（与当前 `app/main.py` 一致）。

在 master（`KUBECONFIG` 指向 k3s）：

```bash
kubectl apply -k monitoring/
kubectl -n monitoring get pods,svc -w
```

安全组入方向建议放行（来源为你的办公/家庭公网 /32）：

- TCP 30090（Prometheus）
- TCP 30300（Grafana）

业务 api 仍用 30080（第 9.4 节）。

访问示例（将 `PUBLIC_IP` 换成 master 公网 IP）：

```bash
PUBLIC_IP=118.31.68.235   # 示例，请替换
echo "Prometheus: http://${PUBLIC_IP}:30090/targets"
echo "Grafana:    http://${PUBLIC_IP}:30300"
```

或使用 `bash tools/cloud/deploy-monitoring.sh`。

### 10.3 截图清单（投简历前）

在云集群完成（公网 IP 为 master 公网地址）：

1. `kubectl -n monitoring get pods` 全部 Running
2. Prometheus → Status → Targets：`cloudlab-api` 为 UP — `http://<公网IP>:30090/targets`
3. Grafana → Dashboards → CloudLab Overview：`app_up` / `mysql_up` / `redis_up` 为 1 — `http://<公网IP>:30300`
4. （可选告警）临时停 MySQL，约 1 分钟后 Alerts 为 FIRING，截完立刻恢复：

```bash
kubectl -n cloudlab scale deployment/mysql --replicas=0
# 截图 http://<公网IP>:30090/alerts
kubectl -n cloudlab scale deployment/mysql --replicas=1
```

将截图保存到 `docs/screenshots/`（**勿提交密钥**），建议文件名：

| 文件 | 内容 |
|------|------|
| `01-prometheus-targets.png` | Targets 中 `cloudlab-api` 为 UP |
| `02-prometheus-alerts.png` | Alerts 页面（可选） |
| `03-grafana-overview.png` | CloudLab Overview 看板 |
| `04-alert-firing.png` | MySQL 缩容后告警 FIRING（可选） |

入口：`http://<公网IP>:30090` / `:30300`（勿用 localhost）。

### 10.4 卸载监控

```bash
kubectl delete -k monitoring/
```

不会删除 `cloudlab` 业务命名空间。

### 10.5 常见故障

| 现象 | 处理 |
|------|------|
| api 没有 `/metrics` | 云上重新 build/import 或 CI 推送含 metrics 的 api 镜像，再 `rollout restart` |
| Targets 里没有 cloudlab-api | 确认 api Pod 注解 `prometheus.io/scrape=true`；Prometheus 与 api 同一 k3s 集群 |
| Grafana 看板无数据 | 等 1～2 个 scrape 周期；先看 Prometheus Targets |
| 浏览器打不开 30090 / 30300 | 检查安全组与公网 IP；勿用 localhost |
| 监控 Pod ImagePullBackOff | 节点预拉监控镜像或配置镜像加速 / import |
| node-exporter 指标为空 | 部分环境 hostPath 受限；业务 `app_up` 仍可截图，Node 图可后补 |

## 11. 仓库目录与脚本

```text
docker_cloud_lab/
  app/                    # FastAPI 应用与 dockerfile
  k8s/                    # 业务 Kubernetes 清单
  monitoring/             # Prometheus、Grafana、告警规则
  tools/cloud/            # 云上部署脚本（在 master 执行）
  tools/optional-local-minikube/  # 可选本机 minikube（默认不用）
  docs/screenshots/       # 监控截图归档（.gitkeep）
  .github/workflows/      # CI 示例 build-push-acr.yml.example
  docker_compose.yaml     # 本机联调
  .env.example            # Compose 环境变量模板
```

| 脚本（`tools/cloud/`） | 作用 |
|------------------------|------|
| `deploy-business.sh` | Namespace + Secret + `kubectl apply -k k8s/` |
| `patch-base-url.sh` | 设置 `BASE_URL`（环境变量 `PUBLIC_IP`） |
| `build-api-image.sh` | 构建 `docker_cloud_lab:1.0` |
| `import-api-on-node.sh` | 当前节点 `k3s ctr images import` |
| （发版） | `kubectl -n cloudlab set image deployment/api api=<VPC>/zhou554_cloudlab/api:<sha>`（见 9.5） |
| `deploy-monitoring.sh` | `kubectl apply -k monitoring/` |

## 12. 附录：可选本机 WSL + minikube

**默认跳过。** 仅在无云资源、想对照清单时使用。

1. WSL 内执行 `docker context use default`（**勿用** `desktop-linux`，否则会 `protocol not available`）。
2. 脚本目录：`tools/optional-local-minikube/`（`export-k8s-images.ps1` + `load-k8s-images-wsl.sh`）。
3. **不要**使用 `eval $(minikube docker-env)` 在 minikube 内 build；Compose 与 K8s 对照见第 8 节。
