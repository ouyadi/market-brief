"""Check enabled Supervisor processes without creating MCP sessions."""

import configparser
import sys
import xmlrpc.client

from supervisor.xmlrpc import SupervisorTransport


def main():
    config = configparser.ConfigParser(interpolation=None)
    config.read('/etc/supervisord.conf')
    expected = {
        section.removeprefix('program:')
        for section in config.sections()
        if section.startswith('program:')
        and config.getboolean(section, 'autostart', fallback=True)
    }
    client = xmlrpc.client.ServerProxy(
        'http://localhost',
        transport=SupervisorTransport(None, None, 'unix:///tmp/supervisor.sock'),
    )
    states = {p['name']: p['statename'] for p in client.supervisor.getAllProcessInfo()}
    failed = sorted(name for name in expected if states.get(name) != 'RUNNING')
    if not expected or failed:
        print('MCP process health failed:', ', '.join(failed) or 'no enabled processes')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
