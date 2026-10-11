"""Read-only SFTP sessions through an optional password-authenticated jump host."""
from contextlib import contextmanager, ExitStack
from pathlib import Path
import threading
import paramiko


def _login(client, endpoint, known_hosts, timeout, sock=None):
    client.load_system_host_keys(str(known_hosts))
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    client.connect(hostname=endpoint.host, port=endpoint.port,
                   username=endpoint.user, password=endpoint.password,
                   look_for_keys=False, allow_agent=False, sock=sock,
                   timeout=timeout, banner_timeout=timeout, auth_timeout=timeout,
                   channel_timeout=timeout)


def _open_sftp(client, timeout):
    # Paramiko's subsystem acknowledgement can wait indefinitely, even after
    # Channel.settimeout(). Closing the transport wakes that wait as well.
    timer = threading.Timer(timeout, client.close)
    timer.daemon = True
    timer.start()
    channel = None
    try:
        channel = client.get_transport().open_session(timeout=timeout)
        channel.settimeout(timeout)
        channel.invoke_subsystem('sftp')
        return paramiko.SFTPClient(channel)
    except BaseException:
        if channel is not None:
            channel.close()
        raise
    finally:
        timer.cancel()


@contextmanager
def open_log_session(profile):
    """Never creates a shell, executes commands, or requests write access."""
    stage = '主机指纹配置'
    try:
        with ExitStack() as cleanup:
            hosts = Path(profile.known_hosts).expanduser()
            if not hosts.is_file():
                raise ValueError('日志 SSH known_hosts 文件不存在，请先登录并核对主机指纹。')
            route = None
            if profile.jump:
                stage = '跳板机登录'
                jump = paramiko.SSHClient()
                cleanup.callback(jump.close)
                _login(jump, profile.jump, hosts, profile.timeout)
                stage = '跳板机到目标服务器转发'
                route = jump.get_transport().open_channel(
                    'direct-tcpip', (profile.target.host, profile.target.port),
                    ('127.0.0.1', 0), timeout=profile.timeout)
                cleanup.callback(route.close)
            stage = '目标服务器登录'
            target = paramiko.SSHClient()
            cleanup.callback(target.close)
            _login(target, profile.target, hosts, profile.timeout, route)
            stage = '目标服务器 SFTP'
            sftp = _open_sftp(target, profile.timeout)
            cleanup.callback(sftp.close)
            sftp.get_channel().settimeout(profile.timeout)
            stage = '日志读取'
            yield sftp
    except paramiko.hostkeys.InvalidHostKey:
        raise ValueError('日志 SSH known_hosts 文件格式错误。') from None
    except paramiko.BadHostKeyException:
        raise ValueError(f'{stage}失败：主机指纹不匹配，请联系管理员核对。') from None
    except paramiko.AuthenticationException:
        raise ValueError(f'{stage}失败：请检查该服务器的 SSH 用户名和密码。') from None
    except PermissionError:
        raise ValueError(f'{stage}失败：账号没有读取该目录或文件的权限。') from None
    except FileNotFoundError:
        raise ValueError(f'{stage}失败：文件或目录不存在，可能已轮转，请重新列出日志文件。') from None
    except (paramiko.SSHException, paramiko.SFTPError, OSError, EOFError, UnicodeError):
        raise ValueError(f'{stage}失败：请检查网络、known_hosts、SFTP 和 TCP 转发权限。') from None
