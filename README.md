# MySQL 与 SSH 日志只读 MCP

本项目采用 [MIT License](LICENSE)，允许使用、修改、分发和商用，分发时需保留版权及许可声明。软件按原样提供，不附带担保。第三方依赖遵循各自许可证。

本项目为 Codex 提供两个独立的 MCP 服务：查询 MySQL 数据库，以及经 SSH/SFTP 读取服务器日志。两者共用本地 Python 虚拟环境和 `.env`，可只启用其中一个，也可同时启用。服务运行在自己的电脑上，无需部署到数据库或日志服务器。

| 服务 | 功能 | 连接方式 | 入口 |
| --- | --- | --- | --- |
| `mysql_readonly` | 列出数据库连接、表、字段，执行单条只读 SQL；面向 MySQL 5.6 | 直连，或经一台 SSH 跳板机访问数据库 | `mysql_readonly.py` |
| `logs_readonly` | 列出日志源和文件、查看末尾、按关键词搜索及返回上下文 | 直连目标服务器，或经一台跳板机访问多个目标服务器 | `logs_readonly.py` |

本文包含安装、配置、账号权限、调用示例与排错步骤。可按需阅读 [MySQL 配置](#配置数据库) 或 [日志 MCP 配置](#日志-mcp)。

## 首次使用：安装项目

准备 Python 3.12（本项目已使用 3.12 验证）、Codex，以及管理员提供的专用只读账号。以下以 Windows PowerShell 为例，在项目目录执行：

```powershell
cd C:\work\mcp
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip check
```

已有 `.venv` 时跳过创建步骤。直接调用虚拟环境的 Python，不需要激活环境。不要复制其他同事的 `.venv`，每台电脑自行创建。

在项目根目录新建 `.env`，按下文填写所需服务的配置。只用 MySQL 时不必填写 `LOG_*`；只用日志时不必填写 `MYSQL_*`。分享项目时排除 `.env`、`.venv`、日志下载文件及缓存，不要分享密码或其他人的 SSH 主机指纹文件。每位同事使用自己的账号和配置。

## 配置如何加载与选择

两个服务都读取 **Python 脚本所在目录的 `.env`**，与 Codex 当前项目目录无关。进程中的同名环境变量优先于 `.env`。配置在首次调用时加载并缓存，修改后需要重启 MCP；直接退出并重新打开 Codex 即可。

`MYSQL_CONNECTIONS=test,prod` 定义数据库连接别名，对应 `MYSQL_TEST_*`、`MYSQL_PROD_*`。`LOG_SERVERS=test,prod` 定义日志服务器别名，对应 `LOG_TEST_*`、`LOG_PROD_*`。两个服务的别名互相独立，调用时明确告诉 Codex 使用哪个服务和别名。

例如：“用 MySQL 的 prod 连接查询 orders”或“查看日志服务器 test 的 app 日志源”。别名不是实际主机名或数据库名；实际地址和数据库名填写在对应配置中。

## 配置数据库

把下面的配置按需填入现有 `.env`。脚本总是读取自身目录下的 `.env`，不依赖 Codex 的工作目录；同名进程环境变量优先。修改 `.env` 后重启 MCP。

```dotenv
MYSQL_CONNECTIONS=main,report
MYSQL_MAIN_HOST=192.168.1.10
MYSQL_MAIN_PORT=3306
MYSQL_MAIN_USER=mcp_reader
MYSQL_MAIN_PASSWORD="your-password"
MYSQL_MAIN_DATABASE=app
MYSQL_REPORT_HOST=192.168.1.20
MYSQL_REPORT_PORT=3306
MYSQL_REPORT_USER=mcp_reader
MYSQL_REPORT_PASSWORD="your-password"
MYSQL_REPORT_DATABASE=report
```

添加连接只需向 `MYSQL_CONNECTIONS` 加入名字并配置对应的 `MYSQL_<大写名字>_*` 变量。名字只能由英文字母、数字、下划线组成，以字母开头，不区分大小写。单库也使用同样格式。密码含 `#`、空格时加引号；空密码需要显式写 `PASSWORD=""`。不要提交 `.env`。

可选全局设置：`MYSQL_MAX_ROWS=500`（上限 10000）、`MYSQL_CONNECT_TIMEOUT=5`、`MYSQL_READ_TIMEOUT=15`（秒）、`MYSQL_MAX_RESULT_BYTES=1048576`（返回 JSON 字节限制）。每个连接可设置 `MYSQL_MAIN_SSL_CA`，启用证书及主机名验证；不配置时连接不使用 TLS。

## SSH 跳板机连接

已有直连配置继续使用。需要隧道的连接额外配置 SSH 用户名、密码；`MYSQL_PROD_HOST` 是跳板机能够访问的数据库地址，如果数据库就在跳板机本机可填写 `127.0.0.1`。

```dotenv
# 把 prod 加入现有 MYSQL_CONNECTIONS 列表，例如 main,prod
MYSQL_PROD_HOST=数据库内网地址
MYSQL_PROD_PORT=3306
MYSQL_PROD_USER=数据库只读账号
MYSQL_PROD_PASSWORD="数据库密码"
MYSQL_PROD_DATABASE=业务库
MYSQL_PROD_SSH_HOST=跳板机地址
MYSQL_PROD_SSH_PORT=22
MYSQL_PROD_SSH_USER=服务器远程用户名
MYSQL_PROD_SSH_PASSWORD="服务器远程密码"
# 可选：默认读取当前用户 ~/.ssh/known_hosts
# MYSQL_PROD_SSH_KNOWN_HOSTS=C:/Users/你的用户名/.ssh/known_hosts
```

首次使用前，在本机运行 `ssh -p 22 服务器远程用户名@跳板机地址`，向管理员核对主机指纹后接受并登录一次，再退出。MCP 使用 known_hosts 校验主机身份，不自动接受陌生主机；非 22 端口需用实际端口登录。自定义 known_hosts 路径建议使用绝对路径。

存在 `SSH_HOST` 时自动通过 SSH TCP 转发访问 MySQL，未配置时直连。跳板机需要允许 TCP 转发，并能访问数据库端口；无需在跳板机安装 MCP，也不会执行远程 shell 命令。SSH 和数据库使用各自的账号密码。每次查询建立隧道，结束或失败后关闭；查询、列出表和查看字段均走相同连接路径。MySQL TLS 配置仍可用，校验证书的数据库主机名使用 `MYSQL_PROD_HOST`。

## 手动启动（仅用于排查）

```powershell
.\.venv\Scripts\python.exe mysql_readonly.py
```

启动后等待 stdio MCP 请求是正常行为，可用 Ctrl+C 退出。正常使用时不需要提前手动启动，Codex 会按配置自动启动进程。不要向标准输出添加调试日志。

## 配置 Codex

将所需服务的配置节追加到当前 Windows 用户的 `%USERPROFILE%\.codex\config.toml`；已有同名节时修改原节，不要重复添加。下面路径全部换成你自己的绝对路径，保留文件内原有其他设置。重启 Codex 后检查工具列表。配置格式参考 [OpenAI 官方 MCP 文档](https://developers.openai.com/codex/mcp)。

```toml
[mcp_servers.mysql_readonly]
command = 'C:\work\mcp\.venv\Scripts\python.exe'
args = ['C:\work\mcp\mysql_readonly.py']
startup_timeout_sec = 20
tool_timeout_sec = 45

[mcp_servers.logs_readonly]
command = 'C:\work\mcp\.venv\Scripts\python.exe'
args = ['C:\work\mcp\logs_readonly.py']
startup_timeout_sec = 20
tool_timeout_sec = 90
```

可以这样向 Codex 提问：“用 mysql_readonly 的 main 连接列出表，查看 users 的字段，再查询最近 20 条记录。”

| MySQL 工具 | 参数与用途 |
| --- | --- |
| `list_connections` | 无参数，列出已配置连接别名 |
| `list_tables` | `connection`：列出默认数据库中的表 |
| `describe_table` | `connection`、`table`：查看字段 |
| `query_sql` | `connection`、`sql`、可选 `max_rows`：执行查询 |

工具参数示例：

```json
{"connection":"main","sql":"SELECT id, name FROM users ORDER BY id DESC LIMIT 20","max_rows":20}
```

结果使用 `columns` 和按列排列的 `rows`，因此重复列名不会丢失数据；附带 `row_count` 和 `truncated`。Decimal 转字符串保留精度，日期用 ISO 格式，二进制值用 `{"base64":"..."}`。

## 只读边界

只允许单条 SELECT 和 UNION；支持 JOIN、聚合和子查询。拒绝 INSERT/UPDATE/DELETE、DDL、CALL、SET、SHOW、EXPLAIN、WITH（MySQL 5.6 不支持）、多语句、变量、文件导出、锁定查询、可执行注释和非白名单函数。需要表/字段信息时使用专用工具。白名单覆盖常用聚合、字符串、日期和数学函数；未覆盖的函数会拒绝，不会默认放行。

每次请求新建连接，执行 `START TRANSACTION READ ONLY`，设置 `SQL_SELECT_LIMIT`，查询结束后回滚并关闭连接。使用解析后重新生成的 SQL，避免直接执行未校验的原始文本。只读事务依据 [MySQL 5.6 官方手册](https://downloads.mysql.com/docs/refman-5.6-en.pdf)。

**必须使用专用 SELECT 账号。** 由数据库管理员执行下面的示例（替换数据库、来源主机和密码），MCP 不执行这些管理语句：

```sql
CREATE USER 'mcp_reader'@'客户端IP' IDENTIFIED BY '替换成强密码';
GRANT SELECT ON `app`.* TO 'mcp_reader'@'客户端IP';
SHOW GRANTS FOR 'mcp_reader'@'客户端IP';
```

不要授予 FILE、EXECUTE、SUPER、CREATE TEMPORARY TABLES 或其他写权限。只读事务存在临时表等例外，SQL 拦截不能替代数据库权限隔离。查询范围由账号 SELECT 权限决定；DATABASE 是默认库，不是跨库访问隔离边界。具有副作用的 UDF/视图应由管理员排除在该账号可访问对象之外。

行数和返回 JSON 字节限制控制输出大小，不能保证扫描量或单个字段的内存大小。MySQL 5.6 没有这里可用的服务端 SELECT 总执行时间限制：读超时限制客户端等待，断开连接不保证服务端立即停止计算。大表请添加过滤条件与 LIMIT。

## 验证

```powershell
.\.venv\Scripts\python.exe -m pip check
```

填入实际配置后，用 `SELECT VERSION()` 和一条小范围 SELECT 做连接验证。

首次验证建议告诉 Codex：“列出 MySQL 连接，然后用 main 执行 SELECT VERSION()”。日志服务则告诉它：“列出日志服务器和日志文件，再读取一个文件最后 10 行”。列出连接别名只验证配置加载；真正查询或列出远程文件才能验证连接，读取正文才能验证文件读取权限。

## 日志 MCP

日志服务独立于 MySQL MCP，使用相同虚拟环境和 `.env`，只读取自己的 `LOG_*` 配置。已有依赖可直接使用，无需额外安装。入口是 `logs_readonly.py`。

### 你需要做的三步

1. 让管理员在跳板机创建 `log_jump`，允许 TCP 转发到目标服务器的 SSH 端口；在每台目标服务器创建 `log_reader`，授予指定日志目录读取和目录遍历权限，不授予 sudo 或写权限。日志轮转后生成的新文件也要可读。目标服务器需要启用 SFTP 子系统。
2. 把下面的配置填入现有 `.env`，替换地址、密码和真实日志目录。不要删除现有 `MYSQL_*` 配置，也不要把密码发到聊天中。
3. 添加 Codex MCP 配置，核对两端 SSH 主机指纹后重启 Codex。

### .env 配置

```dotenv
# 服务器名称可以自行修改；每个名称对应 LOG_<大写名称>_* 一组配置
LOG_SERVERS=order,payment

# 一台跳板机供所有日志目标服务器共用
LOG_JUMP_HOST=跳板机地址
LOG_JUMP_PORT=22
LOG_JUMP_USER=log_jump
LOG_JUMP_PASSWORD="跳板机密码"

# order 目标服务器；app、nginx 是日志别名，不是账号
LOG_ORDER_HOST=目标服务器1内网地址
LOG_ORDER_PORT=22
LOG_ORDER_USER=log_reader
LOG_ORDER_PASSWORD="目标服务器1密码"
LOG_ORDER_DIRS='{"app":"/data/logs","nginx":"/var/log/nginx"}'

# payment 可以有完全不同的目录
LOG_PAYMENT_HOST=目标服务器2内网地址
LOG_PAYMENT_PORT=22
LOG_PAYMENT_USER=log_reader
LOG_PAYMENT_PASSWORD="目标服务器2密码"
LOG_PAYMENT_DIRS='{"app":"/opt/payment/log"}'

# 可选；默认当前 Windows 用户的 ~/.ssh/known_hosts
# LOG_KNOWN_HOSTS=C:/Users/你的用户名/.ssh/known_hosts
# 默认 utf-8，也支持 utf-8-sig、gbk、gb18030、latin-1
# LOG_ORDER_ENCODING=utf-8
# 每次网络操作超时，单位秒
# LOG_SSH_TIMEOUT=10
# 每个文件最多读取 4 MiB，范围 1024~33554432
# LOG_SCAN_BYTES=4194304
# 返回 JSON 上限 64 KiB，范围 1024~1048576
# LOG_OUTPUT_BYTES=65536
# 每次扫描时间预算；阻塞网络操作还受 LOG_SSH_TIMEOUT 限制
# LOG_SCAN_SECONDS=20
```

只有一台目标服务器时，填写 `LOG_SERVERS=order` 和 `LOG_ORDER_*` 即可，不用填写 payment。不经过跳板机时省略全部 `LOG_JUMP_*`。配置在首次调用工具时加载并缓存，修改后重启 MCP。

### 主机指纹

两端都要验证主机身份。首次在 Windows 本机执行以下命令，核对管理员提供的指纹后接受，分别输入跳板机和目标账号密码：

```powershell
sftp -o "ProxyJump=log_jump@跳板机地址:22" -P 22 log_reader@目标服务器内网地址
```

进入 `sftp>` 后执行 `ls /data/logs`（替换实际目录），最后输入 `bye`。若跳板机端口不是 22，修改 ProxyJump 中的端口；目标端口用大写 `-P` 指定。每台目标服务器都需要自己的 known_hosts 记录。使用 Windows 上实际运行 Codex 的用户登录，或用 `LOG_KNOWN_HOSTS` 指定对应文件。程序不会自动接受陌生或变更的主机密钥。

不经过跳板机时使用 `sftp -P 22 log_reader@目标服务器地址`。采用下文的受限账号后，跳板机不允许交互式登录，目标账号只允许 SFTP，因此应使用 SFTP 验证。

### 启用日志服务

在上文“配置 Codex”中添加 `logs_readonly` 配置节，保存后重启 Codex。无需再次添加 MySQL 服务。

### 怎么提问

- “列出日志 MCP 配置了哪些服务器和日志源。”
- “列出 order 服务器 app 日志目录的文件。”
- “查看 order 的 app.log 最后 100 行。”
- “在 order 的 app 日志源中搜索 app.log 的订单号 XXX，返回前后 5 行。”
- “搜索 app.log.1.gz 的 XXX，设置 from_end=false。”

工具是 `list_log_sources`、`list_log_files`、`tail_log`、`search_log`。最后两个工具必须提供 `server`、`source` 和 `file`；文件名来自日志列表。允许目录内相对路径，例如 `2026/app.log`，不接受绝对路径或 `..`。列表仅列出当前目录普通文件，不递归遍历子目录。

```json
{"server":"order","source":"app","file":"app.log","keyword":"订单号","context_lines":5,"max_matches":20,"from_end":true}
```

搜索使用普通文本匹配，默认搜索末尾最多 4 MiB；设置 `from_end=false` 从文件开头搜索。命中按读取窗口中的顺序返回，`line_in_window` 是当前窗口行号，不是完整文件行号。重叠上下文会合并。可用日期或时间文本作关键词，当前版本不解析不同日志格式的时间范围，也不支持正则。

gzip 轮转文件仅支持从头搜索，压缩输入和解压输出都有大小限制；不能对 gzip 使用 tail。`match_count` 是窗口内选取的匹配数，返回字节限制可能进一步减少展示行。`scan_truncated=true` 表示没有扫描完整文件；`truncated=true` 表示扫描范围、匹配数、请求行数或输出大小受到限制。没有命中不能证明整个日志没有记录。

### 权限与限制

程序仅使用 SFTP 的目录列表、路径解析、文件属性和只读打开，不执行远程 shell 命令，不上传、不删除、不修改文件。路径会检查目录范围及符号链接目标。底层调用参照 [Paramiko SFTP 文档](https://docs.paramiko.org/en/stable/api/sftp.html)。

服务器账号权限仍是重要边界：SFTP 路径解析到文件打开之间存在文件系统竞争窗口，MCP 不能代替账号隔离。不要使用 root。避免把凭据文件放在授权日志目录里；日志返回内容也可能包含个人信息和 Token，本版本不自动脱敏。日志正在追加或轮转时，读取结果是一次有限窗口，不保证一致快照；文件消失时重新列出文件后重试。

跳板机账号只需要转发权限；即使禁用了交互式 shell，TCP 转发仍需被 sshd 明确允许。目标账号必须支持 SFTP。此程序不会创建账号或修改服务器权限。

### CentOS 服务器账号设置（管理员操作）

以下由管理员使用 root 执行。示例目标 IP、目录必须换成实际值。账号已存在时跳过 `useradd`，不要重复创建。修改 SSH 配置前保留当前 root 会话、备份配置，检查现有 `Match`、`AllowUsers` 和 `AllowGroups` 是否冲突。配置语法随 OpenSSH 版本而异，以服务器上的 `sshd -t` 结果为准。

#### 跳板机

```bash
useradd -m -s /bin/bash log_jump
passwd log_jump
cp -a /etc/ssh/sshd_config /etc/ssh/sshd_config.before-log-jump
```

在 `/etc/ssh/sshd_config` 末尾追加：

```text
Match User log_jump
    PasswordAuthentication yes
    AllowTcpForwarding local
    PermitOpen 172.16.0.11:22 172.16.0.12:22
    MaxSessions 0
    AllowAgentForwarding no
    X11Forwarding no
    PermitTTY no
```

`PermitOpen` 必须包含实际目标 SSH 地址和端口，与 `.env` 的 `LOG_<名称>_HOST/PORT` 一致；使用域名与使用 IP 是不同的匹配值。新增目标时需要同时更新此处。`MaxSessions 0` 禁止 shell 和 SFTP 会话，同时允许受限 TCP 转发。不要给账号 sudo 或 wheel 权限。

```bash
sshd -t && systemctl reload sshd
```

#### 每台目标服务器

```bash
useradd -m -s /bin/bash log_reader
passwd log_reader
yum install acl

# 替换真实日志目录；本示例日志在 /data/logs
LOG_DIR=/data/logs
```

先执行 `test -d "$LOG_DIR"`，确认成功再继续。以下命令不更改原文件的属主，通过 ACL 为新增账号授予权限：

```bash
getfacl -R -p "$LOG_DIR" > /root/log-reader-acl.backup
getfacl -p /data > /root/log-reader-parent-acl.backup
setfacl -m u:log_reader:--x /data
find "$LOG_DIR" -type d -exec setfacl -m u:log_reader:r-x {} +
find "$LOG_DIR" -type f -exec setfacl -m u:log_reader:r-- {} +
find "$LOG_DIR" -type d -exec setfacl -m d:u:log_reader:r-x {} +
```

目录每一级父路径都需要遍历权限，例如 `/opt/services/order/logs` 要检查 `/opt`、`/opt/services`、`/opt/services/order`。默认 ACL 用于新建文件和子目录；应用主动 chmod 或从其他目录移入轮转文件时可能改变结果，应在日志轮转后再次验证。设置 ACL 可能影响 ACL mask，管理员应核对 `getfacl` 输出中的原有用户、组及 effective 权限。

备份目标服务器 SSH 配置：

```bash
cp -a /etc/ssh/sshd_config /etc/ssh/sshd_config.before-log-reader
```

在 `/etc/ssh/sshd_config` 末尾追加：

```text
Match User log_reader
    PasswordAuthentication yes
    ForceCommand internal-sftp -R
    AllowTcpForwarding no
    AllowAgentForwarding no
    X11Forwarding no
    PermitTTY no
```

```bash
sshd -t && systemctl reload sshd
runuser -u log_reader -- ls /data/logs
runuser -u log_reader -- head -n 2 /data/logs/实际日志文件.log
```

`--` 和 `head` 之间必须有空格。以上测试只验证文件权限，还需要从 Windows 执行前文 SFTP 命令验证完整链路。无需重启机器；正确限定的 Match 配置仅作用于对应账号。重载后另开窗口验证 root 新登录正常，再关闭保留的会话。

`internal-sftp -R` 限制 SFTP 写操作，但 ACL 不等于文件系统隔离：账号仍可能读取系统中其他对所有人可读的文件。若要在服务器端严格限制可见目录，由管理员配置 SFTP chroot；MCP 自身另有目录白名单。SSH 设置参见 [OpenSSH sshd_config](https://man.openbsd.org/sshd_config)，只读选项参见 [sftp-server](https://man.openbsd.org/sftp-server)。

### 超时与常见问题

大文件经过跳板机读取时，可先使用以下显式设置（它们不是程序默认值）：

```dotenv
LOG_SCAN_BYTES=262144
LOG_SCAN_SECONDS=40
LOG_SSH_TIMEOUT=15
```

| 配置 | 默认值 | 用途 |
| --- | --- | --- |
| `LOG_SCAN_BYTES` | 4194304（4 MiB） | 单次读取窗口，tail 和 search 共用；范围 1024～33554432 |
| `LOG_SCAN_SECONDS` | 20 秒 | 扫描时间预算；范围 1～120 |
| `LOG_SSH_TIMEOUT` | 10 秒 | SSH 连接和网络操作超时；范围 1～60 |
| `LOG_OUTPUT_BYTES` | 65536（64 KiB） | 返回 JSON 大小上限；范围 1024～1048576 |
| Codex `tool_timeout_sec` | 示例设置 90 秒 | Codex 等待整个工具调用的时间 |

当前 tail 会先读取受限的末尾窗口再选出指定行数，因此仅把 100 行改成 10 行不一定减少网络传输。调小 `LOG_SCAN_BYTES` 能减少传输，但会同时缩小搜索范围，也可能无法容纳所需的全部行数。扫描预算之外还有连接和阻塞网络等待，Codex 的工具超时应留有余量。修改 `.env` 或 Codex 配置后重启 MCP/Codex。

| 现象 | 检查方向 |
| --- | --- |
| `administratively prohibited` / `stdio forwarding failed` | 跳板机的 AllowTcpForwarding、PermitOpen 是否允许实际目标地址和端口；此错误通常不是密码错误 |
| `Permission denied` | 区分跳板机和目标机密码，检查账号是否允许登录，以及日志文件和父目录 ACL |
| 可以列出目录但不能读文件 | 用 `runuser -u log_reader -- head ...` 检查具体文件的读取权限 |
| SSH shell 无法进入但 SFTP 正常 | 使用受限账号时这是预期行为 |
| 主机密钥未知或变更 | 用本机 SFTP 核实并保存两端指纹，检查 LOG_KNOWN_HOSTS；不要直接关闭验证 |
| 日志扫描超过时间限制 | 先减小 LOG_SCAN_BYTES，再按需提高扫描或网络超时；工具超时也需足够 |
| 返回不足 100 行或没有匹配 | 检查扫描与输出截断标志；无法据此判断整个文件没有更多记录 |
| 新日志文件不可读 | 检查轮转后的文件 ACL、创建方式及程序是否修改权限 |

添加服务器时依次完成：创建账号和日志读取权限 → 更新跳板机 PermitOpen → 本机 SFTP 登录验证并保存指纹 → 增加 LOG_SERVERS 和对应 LOG_<名称>_* → 重启 Codex → 列出文件并读取正文。

## 常见问题

| 现象 | 检查方向 |
| --- | --- |
| MCP 启动失败或没有工具 | 检查 config.toml 绝对路径，确认 `.venv` 已安装 requirements.txt；运行 `pip check` 后重启 Codex |
| 修改 .env 后仍使用旧地址 | 重启 MCP/Codex；检查系统是否存在同名环境变量覆盖配置 |
| MySQL 连接、TLS 或字符编码失败 | 检查主机、端口、账号权限、网络白名单、SSH 配置及 CA；直连成功不代表跳板机路径可用 |
| SSH 主机密钥错误 | 用运行 Codex 的本机用户验证主机指纹；检查 known_hosts 路径，主机变更时先向管理员核实 |
| SQL 函数不在白名单 | 改用支持的函数或由维护者审核后扩展；工具不会自动放行 |
| 日志 SSH 转发被拒绝或扫描超时 | 参见 [日志排错与超时配置](#超时与常见问题) |

## 项目文件

| 文件 | 用途 |
| --- | --- |
| `mysql_readonly.py` | MySQL MCP 入口：创建 FastMCP、注册工具及参数，调用 QueryService |
| `mysql_service.py` | MySQL 配置加载、SQL 校验、函数白名单、只读事务、查询及结果转换 |
| `mysql_ssh.py` | MySQL SSH 隧道、主机指纹校验和连接资源释放 |
| `logs_readonly.py` | 日志 MCP 入口：创建 FastMCP、注册工具及参数，调用 LogService |
| `logs_service.py` | 日志配置加载、目录边界校验、文件列表、末尾读取、关键词搜索和结果限制 |
| `logs_ssh.py` | 日志跳板机与目标 SSH 连接、只读 SFTP 会话和连接资源释放 |
| `requirements.txt` | 固定版本的 Python 依赖 |
| `.env` | 本机私有连接配置，不应共享或提交 |
| `README.md` | 项目安装、两个 MCP 的配置和使用指南 |
| `LICENSE` | 项目源代码的 MIT 开源许可 |

### 命名与调用关系

Python 文件统一使用小写字母和下划线。`*_readonly.py` 是入口，名称与 Codex 中的 MCP 服务一致；`*_service.py` 处理业务逻辑；`*_ssh.py` 管理 SSH 连接。日志相关文件统一使用复数 `logs`。当前规模下文件放在根目录，便于定位和修改。

```text
Codex → mysql_readonly.py → mysql_service.py → mysql_ssh.py（需要隧道时）
Codex → logs_readonly.py  → logs_service.py  → logs_ssh.py
```

服务入口通过 stdio 与 Codex 通信；配置和连接参数由 service 层加载，ssh 层负责连接。改入口文件名后，需要同步修改 Codex config.toml 的 args；修改模块文件名后，需要同步修改 Python 导入。

### 自定义修改从哪里开始

| 修改需求 | 位置与注意事项 |
| --- | --- |
| 增加数据库或日志服务器 | 只改 `.env` 的别名列表和对应字段，通常无需改代码；重启 MCP 后生效 |
| 增加 MCP 工具或修改工具参数 | 修改对应 `*_readonly.py`，并在 service 层实现；同步更新使用说明 |
| 支持新的 SQL 函数 | 查看 `mysql_service.py` 的 `SAFE_FUNCTIONS`、`validate_sql` 和 `_validate_tree`；先确认 MySQL 5.6 支持且没有写入或其他副作用，验证解析后生成的 SQL |
| 调整 SQL 返回格式或查询限制 | 修改 `mysql_service.py`；可配置的行数、超时和字节限制优先通过 `.env` 设置 |
| 优化日志末尾读取、搜索或解析格式 | 修改 `logs_service.py` 的 `LogService` 及读取辅助函数；保留目录校验、读取量和输出大小限制 |
| 修改 SSH 登录方式、跳板链路或连接超时处理 | 修改对应 `*_ssh.py`，同时检查 service 层配置加载；保留主机指纹验证和异常情况下的资源释放 |

修改后先运行 `python -m compileall` 检查语法，再重启 MCP 验证工具发现和小范围真实读取。涉及权限、SQL 校验或路径校验时，还应验证写入 SQL、越界路径及异常连接会被拒绝。不要通过关闭只读校验或主机指纹验证来解决兼容问题，也不要向 stdio 的标准输出打印调试信息。
