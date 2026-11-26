"""LAN discovery advertises routing hints only, never credentials or trust pins."""
import ipaddress
import socket
from zeroconf import ServiceInfo, Zeroconf, IPVersion
import ifaddr
from .features import VERSION

SERVICE = "_mellow._tcp.local."


def addresses(host):
    if host in {"127.0.0.1", "localhost", "::1"}:
        return []
    if host != "0.0.0.0":
        values = [str(entry[4][0]) for entry in socket.getaddrinfo(host, None, socket.AF_INET)]
    else:
        values = [ip.ip for adapter in ifaddr.get_adapters() for ip in adapter.ips if ip.is_IPv4 and isinstance(ip.ip, str)]
    return sorted({v for v in values if not ipaddress.ip_address(v).is_loopback and not ipaddress.ip_address(v).is_unspecified})


class Announcement:
    def __init__(self, host, port, center_id):
        self.zeroconf = None
        self.info = None
        ips = addresses(host)
        if not ips:
            return
        label = "mellow-" + center_id[:12]
        self.info = ServiceInfo(SERVICE, f"{label}.{SERVICE}",
            addresses=[socket.inet_aton(ip) for ip in ips], port=port,
            properties={"center_id": center_id, "version": VERSION, "tls": "required"},
            server=f"{label}.local.")
        self.zeroconf = Zeroconf(ip_version=IPVersion.V4Only)
        try:
            self.zeroconf.register_service(self.info, allow_name_change=True)
        except Exception:
            self.zeroconf.close()
            self.zeroconf = None
            raise

    def close(self):
        if self.zeroconf and self.info:
            try:
                self.zeroconf.unregister_service(self.info)
            finally:
                self.zeroconf.close()
                self.zeroconf = None
