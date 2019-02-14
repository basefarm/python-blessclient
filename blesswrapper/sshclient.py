#!/usr/local/bin/python

import subprocess
import argparse
import os
import sys
import datetime
import re

from blessclient.client import bless, get_region_from_code, get_regions, load_config
from blessclient.bless_config import BlessConfig


def main():
    parser = argparse.ArgumentParser(description='Bless SSH')
    parser.add_argument('host')
    parser.add_argument('cmd', nargs='*')
    parser.add_argument('--nocache', action='store_true')
    parser.add_argument(
        '--config',
        default=None,
        help='Config file for blessclient. Default to ~/.aws/blessclient.cfg'
    )
    parser.add_argument(
        '--download_config',
        action='store_true',
        help='Download blessclient.cfg from S3 bucket. Will overwrite if file already exist'
    )
    parser.add_argument(
        '-4',
        action='store_true',
        help='Forces ssh to use IPv4 addresses only.'
    )
    parser.add_argument(
        '-6',
        action='store_true',
        help='Forces ssh to use IPv6 addresses only.'
    )
    parser.add_argument(
        '-a',
        action='store_true',
        help='Disable forwarding of the authentication agent connection.'
    )
    parser.add_argument(
        '-X',
        action='store_true',
        help='Enables X11 forwarding.'
    )
    parser.add_argument(
        '-Y',
        action='store_true',
        help='Enables trusted X11 forwarding.'
    )
    parser.add_argument(
        '-l',
        default=None,
        help='Specifies the user to log in as on the remote machine. Defaults to IAM user'
    )
    parser.add_argument(
        '-p',
        default=22,
        help='Port to connect to on the remote host. Default 22'
    )
    parser.add_argument(
        '-F',
        default=None,
        help='Specifies an alternative per-user configuration file for ssh.'
    )
    args = parser.parse_args()

    if 'AWS_PROFILE' not in os.environ:
        sys.stderr.write('AWS session not found. Try running get_session first?\n')
        sys.exit(1)

    if 'AWS_EXPIRATION_S' in os.environ:
        expiration = datetime.datetime.fromtimestamp(int(os.environ['AWS_EXPIRATION_S']))
        if expiration < datetime.datetime.now():
            sys.stderr.write('AWS session expired. Try running get_session first?\n')
            sys.exit(1)

    try:
        ssh_version = re.search('OpenSSH_([^\s]+)', subprocess.check_output(['ssh', '-V'], stderr=subprocess.STDOUT).decode('UTF-8'))
        if '7.8' in ssh_version.group(0):
            sys.stderr.write("""@@@@@@@ WARNING @@@@@@@
There is a bug in OpenSSH version 7.8 that makes signed ssh keys not work, and thus Bless does not work.
From our knowledge, the bug only affects the ssh client.
sshd version 7.7 or 7.9 should work with Bless.
We detected that you are running {}
""".format(ssh_version.group(0))+'\n'+'-'*64)
    except Exception as e:
        sys.stderr.write('Failed to get OpenSSH client version\n')

    ssh_options = []
    if vars(args)['4']:
        ssh_options.append('-4')
    if vars(args)['6']:
        if vars(args)['4']:
            sys.stderr.write('ERROR: -4 and -6 are mutually exclusive...\n')
            sys.exit(1)
        ssh_options.append('-6')
    if not args.a:
        ssh_options.append('-A')
    if args.X:
        ssh_options.append('-X')
    if args.Y:
        ssh_options.append('-Y')
    if args.F is not None:
        ssh_options.append('-F')
        ssh_options.append(args.F)

    hostname = None
    username = None
    port = args.p

    host = args.host.split('@')
    if len(host) == 2:
        hostname = host[1]
        username = host[0]
    else:
        hostname = host[0]

    host = hostname.split(':')
    if len(host) == 2:
        hostname = host[0]
        port = host[1]

    ssh_options.append('-p')
    ssh_options.append(str(port))

    if args.l is not None:
        username = args.l

    blessclient_output = []
    bless_config = BlessConfig()

    if load_config(bless_config, args.config, args.download_config) is False:
        sys.exit(1)

    start_region = get_region_from_code(None, bless_config)
    for region in get_regions(start_region, bless_config):
        try:
            os.environ['BLESSQUIET'] = "1"
            blessclient_output = bless(region, args.nocache, False, hostname, bless_config, username)
            break
        except SystemExit:
            pass

    if blessclient_output == []:
        sys.exit(1)

    if 'username' in blessclient_output:
        ssh_options.append('-l')
        ssh_options.append(blessclient_output['username'])
    elif username is not None:
        ssh_options.append('-l')
        ssh_options.append(username)

    if len(args.cmd) >= 1:
        for cmd in args.cmd:
            ssh_options.append(cmd)

    subprocess.call(['ssh', hostname] + ssh_options)
