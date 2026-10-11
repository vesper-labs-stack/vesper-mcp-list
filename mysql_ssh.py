"""Password SSH forwarding; exposes a real socket so MySQL TLS still works."""
from contextlib import contextmanager
from pathlib import Path
import select
import socket
import threading


def _relay(local, channel, stopped):
    try:
        while not stopped.is_set():
            ready, _, _ = select.select([local, channel], [], [], 0.2)
            for source in ready:
                data = source.recv(65536)
                if not data:
                    return
                target = channel if source is local else local
                target.sendall(data)
    except (OSError, EOFError, ValueError):
        # EOF propagates to the MySQL driver as a redacted connection error.
        pass
    finally:
        local.close()


@contextmanager
def mysql_socket(profile):
    try:
        import paramiko
    except ImportError:
        raise ValueError('SSH 连接需要 paramiko，请先安装 requirements.txt 中的依赖。') from None
    client = paramiko.SSHClient()
    channel = local = relay_socket = worker = None
    stopped = threading.Event()
    try:
        known_hosts = Path(profile.ssh_known_hosts).expanduser() if profile.ssh_known_hosts else Path.home() / '.ssh' / 'known_hosts'
        if not known_hosts.is_file():
            raise ValueError('SSH known_hosts 文件不存在，请先用 ssh 登录并核对跳板机指纹，或配置 SSH_KNOWN_HOSTS。')
        client.load_system_host_keys(str(known_hosts))
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
        client.connect(
            hostname=profile.ssh_host, port=profile.ssh_port,
            username=profile.ssh_user, password=profile.ssh_password,
            allow_agent=False, look_for_keys=False,
            timeout=profile.connect_timeout, banner_timeout=profile.connect_timeout,
            auth_timeout=profile.connect_timeout,
        )
        transport = client.get_transport()
        if transport is None or not transport.is_active():
            raise ValueError('SSH 会话未建立。')
        channel = transport.open_channel(
            'direct-tcpip', (profile.host, profile.port), ('127.0.0.1', 0),
            timeout=profile.connect_timeout,
        )
        channel.settimeout(profile.read_timeout)
        local, relay_socket = socket.socketpair()
        relay_socket.settimeout(profile.read_timeout)
        worker = threading.Thread(target=_relay, args=(relay_socket, channel, stopped), daemon=True)
        worker.start()
        yield local
    except paramiko.hostkeys.InvalidHostKey:
        raise ValueError('SSH known_hosts 文件格式错误，请检查主机公钥记录。') from None
    except paramiko.AuthenticationException:
        raise ValueError('SSH 认证失败，请检查 SSH 用户名和密码。') from None
    except paramiko.BadHostKeyException:
        raise ValueError('SSH 主机指纹不匹配，请联系管理员核对 known_hosts。') from None
    except (paramiko.SSHException, OSError, EOFError):
        raise ValueError('SSH 隧道失败，请检查网络、known_hosts 主机指纹及跳板机 TCP 转发权限。') from None
    finally:
        stopped.set()
        if local is not None:
            local.close()
        if channel is not None:
            channel.close()
        if relay_socket is not None:
            relay_socket.close()
        client.close()
        if worker is not None:
            worker.join(timeout=1)
