#!/usr/local/bin/python
from __future__ import absolute_import
import boto3
from botocore.exceptions import (ClientError,
                                 ConnectionError,
                                 EndpointConnectionError)

import kmsauth
import os
import sys
import psutil
import datetime
import time
import re
import argparse
import copy
import subprocess
import json
import hvac
import getpass
import socket
from Cryptodome.PublicKey import RSA

import six

from . import awsmfautils
from .bless_aws import BlessAWS
from .bless_cache import BlessCache
from .user_ip import UserIP
from .bless_lambda import BlessLambda
from .housekeeper_lambda import HousekeeperLambda
from .bless_config import BlessConfig
from .vault_ca import VaultCA
from .lambda_invocation_exception import LambdaInvocationException

import logging

try:
    from . import tokengui
except ImportError:
    tokengui = None


DATETIME_STRING_FORMAT = '%Y%m%dT%H%M%SZ'


def update_client(bless_cache, bless_config):
    last_updated_cache = bless_cache.get('last_updated')
    if last_updated_cache:
        last_updated = datetime.datetime.strptime(last_updated_cache, DATETIME_STRING_FORMAT)
    else:
        last_updated = datetime.datetime.utcnow()
        bless_cache.set('last_updated', last_updated.strftime(DATETIME_STRING_FORMAT))
        bless_cache.save()
    if last_updated + datetime.timedelta(days=7) > datetime.datetime.utcnow():
        logging.debug('Client does not need to upgrade yet.')
        return
    # Update
    logging.info('Client is autoupdating.')
    autoupdate_script = bless_config.get_client_config()['update_script']
    if autoupdate_script:
        command = os.path.normpath(os.path.join(
            os.path.dirname(os.path.realpath(__file__)),
            os.pardir,
            autoupdate_script))
        if os.path.isfile(command):
            error_file = open('{}/.blessclient_err.log'.format(os.path.expanduser('~')), 'w')
            subprocess.Popen(command, stdout=error_file, stderr=error_file)
            # Note: Updating will remove the bless cache so the following cache update.
            # We are just setting it in case the update fails or the repo is already up to date
            # in which case the bless cache will not be deleted.
            last_updated = datetime.datetime.utcnow()
            bless_cache.set('last_updated', last_updated.strftime(DATETIME_STRING_FORMAT))
            bless_cache.save()
        else:
            logging.warn('Missing autoupdate script {}'.format(command))
    else:
        logging.info('No update script is configured, client will not autoupdate.')


def is_valid_ipv4_address(address):
    try:
        socket.inet_pton(socket.AF_INET, address)
    except AttributeError:  # no inet_pton here, sorry
        try:
            socket.inet_aton(address)
        except socket.error:
            return False
        return address.count('.') == 3
    except socket.error:  # not a valid address
        return False

    return True


def is_valid_ipv6_address(address):
    try:
        socket.inet_pton(socket.AF_INET6, address)
    except socket.error:  # not a valid address
        return False
    return True


def get_region_from_code(region_code, bless_config):
    if region_code is None:
        region_code = tuple(sorted(bless_config.get('REGION_ALIAS')))[0]
    alias_code = region_code.upper()
    aliases = bless_config.get('REGION_ALIAS')
    if alias_code in aliases:
        return aliases[alias_code]
    else:
        raise ValueError('Unrecognized region code: {}'.format(region_code))


def get_regions(region, bless_config):
    """ Get an ordered list of regions in which to run bless_config
    Args:
        region (str): the current AWS region code (e.g., 'us-east-1') where blessclient
            has attempted to run and failed.
        bless_config (dict): config from BlessConfig
    Returns:
        List of regions
    """
    regions = []
    aws_regions = tuple(sorted(bless_config.get('REGION_ALIAS').values()))
    try:
        ndx = aws_regions.index(region)
    except ValueError:
        ndx = 0
    while len(regions) < len(aws_regions):
        regions.append(aws_regions[ndx])
        ndx = (ndx + 1) % len(aws_regions)
    return regions


def get_kmsauth_config(region, bless_config):
    """ Return the kmsauth config values for a given AWS region
    Args:
        region (str): the AWS region code (e.g., 'us-east-1')
        bless_config (BlessConfig): config from BlessConfig
    Retruns:
        A dict of configuation values
    """
    alias_code = bless_config.get_region_alias_from_aws_region(region)
    return bless_config.get('KMSAUTH_CONFIG_{}'.format(alias_code))


def get_housekeeper_config(region, bless_config):
    """ Return the housekeeper config values for a given AWS region
    Args:
        region (str): the AWS region code (e.g., 'us-east-1')
        bless_config (BlessConfig): config from BlessConfig
    Retruns:
        A dict of configuation values
    """
    try:
        alias_code = bless_config.get_region_alias_from_aws_region(region)
        return bless_config.get('HOUSEKEEPER_CONFIG_{}'.format(alias_code))
    except Exception as e:
        return None


def get_housekeeperrole_credentials(iam_client, creds, housekeeper_config, blessconfig, bless_cache):
    """
    Args:
        iam_client: boto3 iam client
        creds: User credentials with rights to assume the use-bless role, or None for boto to
            use its default search
        blessconfig: BlessConfig object
        bless_cache: BlessCache object
    """
    role_creds = uncache_creds(bless_cache.get('housekeeperrole_creds'))
    if role_creds and role_creds['Expiration'] > time.gmtime():
        return role_creds

    if creds is not None:
        mfa_sts_client = boto3.client(
            'sts',
            aws_access_key_id=creds['AccessKeyId'],
            aws_secret_access_key=creds['SecretAccessKey'],
            aws_session_token=creds['SessionToken']
        )
    else:
        mfa_sts_client = boto3.client('sts')

    if 'AWS_USER' in os.environ:
        user_arn = os.environ['AWS_USER']
    else:
        user = iam_client.get_user()['User']
        user_arn = user['Arn']

    role_arn = awsmfautils.get_role_arn(
        user_arn,
        housekeeper_config['userrole'],
        housekeeper_config['accountid']
    )

    logging.debug("Role Arn: {}".format(role_arn))

    role_creds = mfa_sts_client.assume_role(
        RoleArn=role_arn,
        RoleSessionName='mfaassume',
        DurationSeconds=blessconfig.get_client_config()['usebless_role_session_length'],
    )['Credentials']

    logging.debug("Role Credentials: {}".format(role_creds))
    bless_cache.set('housekeeperrole_creds', make_cachable_creds(role_creds))
    bless_cache.save()

    return role_creds


def get_blessrole_credentials(iam_client, creds, blessconfig, bless_cache):
    """
    Args:
        iam_client: boto3 iam client
        creds: User credentials with rights to assume the use-bless role, or None for boto to
            use its default search
        blessconfig: BlessConfig object
        bless_cache: BlessCache object
    """
    role_creds = uncache_creds(bless_cache.get('blessrole_creds'))
    if role_creds and role_creds['Expiration'] > time.gmtime():
        return role_creds

    lambda_config = blessconfig.get_lambda_config()
    if creds is not None:
        mfa_sts_client = boto3.client(
            'sts',
            aws_access_key_id=creds['AccessKeyId'],
            aws_secret_access_key=creds['SecretAccessKey'],
            aws_session_token=creds['SessionToken']
        )
    else:
        mfa_sts_client = boto3.client('sts')

    user_arn = bless_cache.get('userarn')
    if not user_arn:
        user = iam_client.get_user()['User']
        user_arn = user['Arn']
        bless_cache.set('username', user['UserName'])
        bless_cache.set('userarn', user_arn)
        bless_cache.save()

    role_arn = awsmfautils.get_role_arn(
        user_arn,
        lambda_config['userrole'],
        lambda_config['accountid']
    )

    logging.debug("Role Arn: {}".format(role_arn))

    role_creds = mfa_sts_client.assume_role(
        RoleArn=role_arn,
        RoleSessionName='mfaassume',
        DurationSeconds=blessconfig.get_client_config()['usebless_role_session_length'],
    )['Credentials']

    logging.debug("Role Credentials: {}".format(role_creds))
    bless_cache.set('blessrole_creds', make_cachable_creds(role_creds))
    bless_cache.save()

    return role_creds


def get_idfile_from_cmdline(cmdline, default):
    identity_file = default

    if ('BLESS_IDENTITYFILE' in os.environ) and (
            os.environ['BLESS_IDENTITYFILE'] != ''):
        return os.environ['BLESS_IDENTITYFILE']

    try:
        iflag = cmdline.index('-i')
        identity_file = cmdline[iflag + 1]
    except ValueError:
        pass

    if (identity_file[-4:] == '.pub'):
        # someone set their public key as their identity
        identity_file = identity_file[0:-4]

    return identity_file


def get_mfa_token_cli():
    sys.stderr.write('Enter your AWS MFA code: ')
    mfa_pin = six.moves.input()
    return mfa_pin


def get_mfa_token_gui(message):
    sys.stderr.write(
        "Enter your AWS MFA token in the gui dialog. Alternatively, run mfa.sh first.\n")
    tig = tokengui.TokenInputGUI()
    if message == 'BLESS':
        message = None
    tig.doGUI(message)
    mfa_pin = tig.code
    return mfa_pin


def get_mfa_token(showgui, message):
    mfa_token = None
    if not showgui:
        mfa_token = get_mfa_token_cli()
    elif tokengui:
        mfa_token = get_mfa_token_gui(message)
    else:
        raise RuntimeError(
            '--gui requested but no tkinter support '
            '(often the `python-tk` package).'
        )
    return mfa_token


def clear_kmsauth_token_cache(config, cache):
    cache_key = 'kmsauth-{}'.format(config['awsregion'])
    kmsauth_cache = {
        'token': None,
        'Expiration': '20160101T000000Z'
    }
    cache.set(cache_key, kmsauth_cache)
    cache.save()


def get_kmsauth_token(creds, config, username, cache):
    cache_key = 'kmsauth-{}'.format(config['awsregion'])
    kmsauth_cache = cache.get(cache_key)
    if kmsauth_cache:
        expiration = time.strptime(
            kmsauth_cache['Expiration'], '%Y%m%dT%H%M%SZ')
        if expiration > time.gmtime() and kmsauth_cache['token'] is not None:
            logging.debug(
                'Using cached kmsauth token, good until {}'.format(kmsauth_cache['Expiration']))
            return kmsauth_cache['token']

    config['context'].update({'from': username})
    try:
        token = kmsauth.KMSTokenGenerator(
            config['kmskey'],
            config['context'],
            config['awsregion'],
            aws_creds=creds,
            token_lifetime=60
        ).get_token().decode('US-ASCII')
    except kmsauth.ServiceConnectionError:
        logging.debug("Network failure for kmsauth")
        raise LambdaInvocationException('Connection error getting kmsauth token.')
    # We have to manually calculate expiration the same way kmsauth does
    lifetime = 60 - (kmsauth.TOKEN_SKEW * 2)
    if lifetime > 0:
        expiration = datetime.datetime.utcnow() + datetime.timedelta(minutes=lifetime)
        kmsauth_cache = {
            'token': token,
            'Expiration': expiration.strftime('%Y%m%dT%H%M%SZ')
        }
        cache.set(cache_key, kmsauth_cache)
        cache.save()
    return token


def setup_logging():
    setting = os.getenv('BLESSDEBUG', '')
    if setting == '1':
        logging.basicConfig(level=logging.DEBUG)
    elif setting != '':
        logging.basicConfig(filename=setting, level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.CRITICAL)


def get_bless_cache(nocache, bless_config):
    client_config = bless_config.get_client_config()
    cachedir = os.path.join(
        os.path.expanduser('~'),
        client_config['cache_dir'])
    cachemode = BlessCache.CACHEMODE_RECACHE if nocache else BlessCache.CACHEMODE_ENABLED
    return BlessCache(cachedir, client_config['cache_file'], cachemode)


def make_cachable_creds(token_data):
    _token_data = copy.deepcopy(token_data)
    expiration = token_data['Expiration'].strftime('%Y%m%dT%H%M%SZ')
    _token_data['Expiration'] = expiration
    return _token_data


def uncache_creds(cached_data):
    if cached_data and 'Expiration' in cached_data.keys():
        _cached_data = copy.deepcopy(cached_data)
        _cached_data['Expiration'] = time.strptime(
            cached_data['Expiration'], '%Y%m%dT%H%M%SZ')
        return _cached_data
    return cached_data


def load_cached_creds(bless_config):
    """ Load cached AWS credentials for the user that has recently MFA'ed
        or try from environment variables if use_env_creds is True
    Args:
        bless_config (BlessConfig): Loaded BlessConfig
    Return:
        dict of AWS credentials, or {} if no current credentials are found
    """
    client_config = bless_config.get_client_config()
    cachedir = os.path.join(
        os.getenv(
            'HOME',
            os.getcwd()),
        client_config['mfa_cache_dir'])
    cache_file_path = os.path.join(cachedir, client_config['mfa_cache_file'])
    if not os.path.isfile(cache_file_path):
        return {}

    cached_data = {}
    with open(cache_file_path, 'r') as cache:
        cached_data = uncache_creds(json.load(cache))
        if cached_data['Expiration'] < time.gmtime():
            cached_data = {}
    return cached_data


def save_cached_creds(token_data, bless_config):
    """ Save the session credentials for this user, after the user has MFA'ed
    Args:
        token_data (dict): credentials returned from sts call
        bless_config (BlessConfig): Loaded BlessConfig
    """
    client_config = bless_config.get_client_config()
    cachedir = os.path.join(
        os.getenv(
            'HOME',
            os.getcwd()),
        client_config['mfa_cache_dir'])
    if not os.path.exists(cachedir):
        os.makedirs(cachedir)

    cache_file_path = os.path.join(cachedir, client_config['mfa_cache_file'])
    _token_data = make_cachable_creds(token_data)
    with open(cache_file_path, 'w') as cache:
        json.dump(_token_data, cache)


def ssh_agent_remove_bless(identity_file):
    DEVNULL = open(os.devnull, 'w')
    try:
        current = subprocess.check_output(['ssh-add', '-l']).decode('UTF-8')
        match = re.search(re.escape(identity_file), current)
        if match:
            subprocess.check_call(
                ['ssh-add', '-d', identity_file], stderr=DEVNULL)
    except subprocess.CalledProcessError:
        logging.debug(
            "Non-zero exit from ssh-add, are there no identities in the current agent?")


def ssh_agent_add_bless(identity_file):
    DEVNULL = open(os.devnull, 'w')
    subprocess.check_call(['ssh-add', identity_file], stderr=DEVNULL)
    fingerprint = re.search('SHA256:([^\s]+)', subprocess.check_output(['ssh-keygen', '-lf', identity_file]).decode('UTF-8'))
    if fingerprint is None:
        logging.debug("Could not add '{}' to ssh-agent".format(identity_file))
        sys.stderr.write(
            "Couldn't add identity to ssh-agent\n")
        return
    logging.debug("Fingerprint of cert added: {}".format(fingerprint.group(0)))
    current = subprocess.check_output(['ssh-add', '-l']).decode('UTF-8')
    if not re.search(re.escape(fingerprint.group(0)), current):
        logging.debug("Could not add '{}' to ssh-agent".format(identity_file))
        sys.stderr.write(
            "Couldn't add identity to ssh-agent")


def generate_ssh_key(identity_file, public_key_file):
    ssh_folder = os.path.dirname(identity_file)
    if not os.path.exists(ssh_folder):
        sys.stderr.write("Creating folder {}\n".format(ssh_folder))
        os.makedirs(ssh_folder)

    sys.stderr.write("Generating ssh key ({} - {})\n".format(identity_file, public_key_file))
    key = RSA.generate(4096)
    f = open(identity_file, "wb")
    os.chmod(identity_file, 0o600)
    f.write(key.exportKey('PEM'))
    f.close()

    pubkey = key.publickey()
    f = open(public_key_file, "wb")
    f.write(pubkey.exportKey('OpenSSH'))
    f.close()


def get_stderr_feedback():
    feedback = True
    if os.getenv('BLESSQUIET', '') != '':
        feedback = False
    return feedback


def get_username(aws, bless_cache):
    username = bless_cache.get('username')
    if not username:
        if 'AWS_USER' in os.environ:
            username = os.environ['AWS_USER'].split('/')[1]
    if not username:
        try:
            user = aws.iam_client().get_user()['User']
        except ClientError:
            try:
                awsmfautils.unset_token()
                user = aws.iam_client().get_user()['User']
            except ClientError as e:
                if e.response.get('Error', {}).get('Code') == 'SignatureDoesNotMatch':
                    sys.stderr.write(
                        "Your authentication signature was rejected by AWS; try checking your system " +
                        "date & timezone settings are correct\n")
                    raise

                sys.stderr.write(
                    "Can't get your user information from AWS! Either you don't have your user"
                    " aws credentials set as [default] in ~/.aws/credentials, or you have another"
                    " process setting AWS credentials for a service account in your environment.\n")
                raise
        username = user['UserName']
        bless_cache.set('username', username)
        bless_cache.set('userarn', user['Arn'])
        bless_cache.save()
    return username


def check_fresh_cert(cert_file, blessconfig, bless_cache, userIP, ip_list=None):
    if os.path.isfile(cert_file):
        certlife = time.time() - os.path.getmtime(cert_file)
        if certlife < float(blessconfig['certlifetime'] - 15):
            if (certlife < float(blessconfig['ipcachelifetime'])
                or bless_cache.get('certip') == userIP.getIP()
            ):
                if ip_list is None or ip_list == bless_cache.get('bastion_ips'):
                    return True
    return False


def load_config(bless_config, config_filename=None, force_download_config=False, s3_bucket=None):
    """
    Returns (boolean):
    """
    if force_download_config:
        if download_config_from_s3(s3_bucket, config_filename) is False:
            return False

    if config_filename is None:
        config_filename = get_default_config_filename()
    try:
        with open(config_filename, 'r') as f:
            bless_config.set_config(bless_config.parse_config_file(f))
    except FileNotFoundError as e:
        if config_filename is None:
            if download_config_from_s3():
                home_dir = os.path.expanduser("~")
                config_filename = os.path.normpath(os.path.join(home_dir, '.aws', 'blessclient.cfg'))
                try:
                    with open(config_filename, 'r') as f:
                        bless_config.set_config(bless_config.parse_config_file(f))
                except FileNotFoundError:
                    pass
        if bless_config.get_config() is None:
            sys.stderr.write('{}\n'.format(e))
            return False
    return True


def download_config_from_s3(s3_bucket=None, file_location=None):
    """ Download blessclient.cfg from S3 bucket
    Returns (boolean):
    """
    try:
        if s3_bucket is None:
            if 'AWS_PROFILE' not in os.environ:
                profile = subprocess.check_output(
                    ['aws', 'configure', 'get', 'default.session_tool_default_profile']).rstrip().decode('utf-8')
            else:
                profile = os.environ['AWS_PROFILE']
            s3_bucket = subprocess.check_output(
                ['aws', 'configure', 'get', '{}.session-tool_bucketname'.format(profile)]).rstrip().decode('utf-8')

        if file_location is None:
            home_dir = os.path.expanduser("~")
            file_location = os.path.normpath(os.path.join(home_dir, '.aws', 'blessclient.cfg'))

        s3 = boto3.resource('s3')
        s3.meta.client.download_file(s3_bucket, 'blessclient/blessclient.cfg', file_location)
        sys.stderr.write('Downloaded blessclient.cfg from {} to {}\n'.format(s3_bucket, file_location))
        return True
    except Exception as e:
        sys.stderr.write('S3: {}\n'.format(e))
        sys.stderr.write('Failed to download blessclient.cfg from {}/{}\n'.format(s3_bucket, 'blessclient/blessclient.cfg'))
        return False


def get_default_config_filename():
    """ Get the full path to the default config file. /etc/blessclient or ~/.aws/blessclient.cfg
    Returns (str): Full path to file blessclient.cfg
    """
    home_dir = os.path.expanduser("~")
    home_config = os.path.normpath(os.path.join(home_dir, '.aws', 'blessclient.cfg'))
    etc_config = "/etc/blessclient/blessclient.cfg"
    if os.path.isfile(home_config):
        return home_config
    if os.path.isfile(etc_config):
        return etc_config
    return home_config


def update_config_from_env(bless_config):
    """ Override config values from environment variables
    Args:
        bless_config (BlessConfig): Loaded BlessConfig
    """
    lifetime = os.getenv('BLESSIPCACHELIFETIME')
    if lifetime is not None:
        lifetime = int(lifetime)
        logging.debug('Overriding ipcachelifetime from env: {}'.format(lifetime))
        bless_config.set_lambda_config('ipcachelifetime', lifetime)


def get_linux_username(username):
    """
    Returns a linux safe username.
    :param username: Name of the user (could include @domain.com)
    :return: Username string that complies with IEEE Std 1003.1-2001
    """
    match = re.search('[.a-zA-Z]+', username)
    return match.group(0)


def get_cached_auth_token(bless_cache):
    """
    Returns cached Vault auth token if available, otherwise None
    :param bless_cache: Bless cache object
    :return: Vault auth token or None if no valid token cached
    """
    vault_creds = bless_cache.get('vault_creds')
    if vault_creds is None or vault_creds['expiration'] is None:
        return None
    else:
        expiration = vault_creds['expiration']
        if datetime.datetime.utcnow() < datetime.datetime.strptime(expiration, '%Y%m%dT%H%M%SZ'):
            logging.debug(
                'Using cached vault token, good until {}'.format(expiration))
            return vault_creds['token']
        else:
            return None


def get_credentials():
    print("Enter Vault username:")
    username = six.moves.input()
    password = getpass.getpass(prompt="Password (will be hidden):")
    return username, password


def get_env_creds(aws, client_config, kmsauth_config, username, bless_cache, bless_config):
    role_creds = None
    if client_config['use_env_creds']:
        env_vars = {
            'AWS_SECRET_ACCESS_KEY': 'SecretAccessKey',
            'AWS_ACCESS_KEY_ID': 'AccessKeyId',
            'AWS_EXPIRATION_S': 'Expiration',
            'AWS_SESSION_TOKEN': 'SessionToken'
        }
        if all(x in os.environ for x in env_vars):
            creds = {}
            for env_var in env_vars.keys():
                creds[env_vars[env_var]] = os.environ[env_var]
                if env_var == 'AWS_EXPIRATION_S':
                    expiration = datetime.datetime.fromtimestamp(int(os.environ[env_var]))
                    creds[env_vars[env_var]] = expiration.strftime(DATETIME_STRING_FORMAT)
            if expiration < datetime.datetime.now():
                creds = None
        try:
            # Try doing this with our env's creds
            kmsauth_token = get_kmsauth_token(
                None,
                kmsauth_config,
                username,
                cache=bless_cache
            )
            logging.debug(
                "Got kmsauth token by env creds: {}".format(kmsauth_token))
            role_creds = get_blessrole_credentials(
                aws.iam_client(), creds, bless_config, bless_cache)
            logging.debug("Env creds used to assume role use-bless")
        except Exception as e:
            logging.debug('Failed to use env creds: {}'.format(e))
            pass

    if role_creds is None:
        return [None, None, None]

    return [creds, role_creds, kmsauth_token]


def auth_okta(client, auth_mount, bless_cache):
    """
    Authenticates a user in HashiCorp Vault using Okta
    :param bless_cache: Bless Cache to cache auth token
    :param auth_mount: Authentication mount point on Vault
    :param client: HashiCorp Vault client
    :return: Updated HashiCorp Vault client, and linux username
    """

    vault_auth_token = get_cached_auth_token(bless_cache)
    if vault_auth_token is not None:
        client.token = vault_auth_token
        username = get_linux_username(bless_cache.get('vault_creds')['username'])
        return client, get_linux_username(username)
    else:
        username, password = get_credentials()
        auth_params = {
            'password': password
        }
        current_time = datetime.datetime.utcnow()
        auth_url = '/v1/auth/{0}/login/{1}'.format(auth_mount, username)
        response = client.auth(auth_url, json=auth_params)

        token = response['auth']['client_token']
        expiration = current_time + datetime.timedelta(seconds=response['auth']['lease_duration'])
        username = get_linux_username(response['auth']['metadata']['username'])
        vault_credentials_cache = {
            "token": token,
            "expiration": expiration.strftime('%Y%m%dT%H%M%SZ'),
            "username": username
        }
        bless_cache.set('vault_creds', vault_credentials_cache)
        bless_cache.save()
        return client, get_linux_username(username)


def vault_bless(nocache, bless_config):

    vault_addr = bless_config.get('VAULT_CONFIG')['vault_addr']
    auth_mount = bless_config.get('VAULT_CONFIG')['auth_mount']
    bless_cache = get_bless_cache(nocache, bless_config)
    bless_lambda_config = bless_config.get_lambda_config()

    user_ip = UserIP(
        bless_cache=bless_cache,
        maxcachetime=bless_lambda_config['ipcachelifetime'],
        ip_urls=bless_config.get_client_config()['ip_urls'],
        fixed_ip=os.getenv('BLESSFIXEDIP', False))

    # Print feedback?
    show_feedback = get_stderr_feedback()

    # Create client to connect to HashiCorp Vault
    client = hvac.Client(url=vault_addr)

    # Identify the SSH key to be used
    clistring = psutil.Process(os.getppid()).cmdline()
    identity_file = get_idfile_from_cmdline(
        clistring,
        os.getenv('HOME', os.getcwd()) + '/.ssh/blessid'
    )
    # Define the certificate to be created
    cert_file = identity_file + '-cert.pub'

    logging.debug("Using identity file: {}".format(identity_file))

    # Check if we can skip asking for MFA code
    if nocache is not True:
        if check_fresh_cert(cert_file, bless_lambda_config, bless_cache, user_ip):
            logging.debug("Already have fresh cert")
            sys.exit(0)

    # Print feedback information
    if show_feedback:
        sys.stderr.write(
            "Requesting certificate for your public key"
            + " (set BLESSQUIET=1 to suppress these messages)\n"
        )

    # Identify and load the public key to be signed
    public_key_file = identity_file + '.pub'
    with open(public_key_file, 'r') as f:
        public_key = f.read()

    # Only sign public keys in correct format.
    if public_key[:8] != 'ssh-rsa ':
        raise Exception(
            'Refusing to bless {}. Probably not an identity file.'.format(identity_file))

    # Authenticate user with HashiCorp Vault
    client, linux_username = auth_okta(client, auth_mount, bless_cache)

    payload = {
        'valid_principals': linux_username,
        'public_key': public_key,
        'ttl': bless_config.get('BLESS_CONFIG')['certlifetime'],
        'ssh_backend_mount': bless_config.get('VAULT_CONFIG')['ssh_backend_mount'],
        'ssh_backend_role': bless_config.get('VAULT_CONFIG')['ssh_backend_role']
    }

    vault_ca = VaultCA(client)
    try:
        cert = vault_ca.getCert(payload)
    except hvac.exceptions.Forbidden:
        bless_cache = get_bless_cache(True, bless_config)
        client, linux_username = auth_okta(client, auth_mount, bless_cache)

        payload = {
            'valid_principals': linux_username,
            'public_key': public_key,
            'ttl': bless_config.get('BLESS_CONFIG')['certlifetime'],
            'ssh_backend_mount': bless_config.get('VAULT_CONFIG')['ssh_backend_mount'],
            'ssh_backend_role': bless_config.get('VAULT_CONFIG')['ssh_backend_role']
        }

        vault_ca = VaultCA(client)
        cert = vault_ca.getCert(payload)

    logging.debug("Got back cert: {}".format(cert))

    # Error handling
    if cert[:29] != 'ssh-rsa-cert-v01@openssh.com ':
        error_msg = json.loads(cert)
        if ('errorType' in error_msg
            and error_msg['errorType'] == 'KMSAuthValidationError'
            and nocache is False
        ):
            logging.debug("KMSAuth error with cached token, purging cache.")
            # clear_kmsauth_token_cache(kmsauth_config, bless_cache)
            raise LambdaInvocationException('KMSAuth validation error')

        if ('errorType' in error_msg and
                error_msg['errorType'] == 'ClientError'):
            raise LambdaInvocationException(
                'The BLESS lambda experienced a client error. Consider trying in a different region.'
            )

        if ('errorType' in error_msg and
                error_msg['errorType'] == 'InputValidationError'):
            raise Exception(
                'The input to the BLESS lambda is invalid. '
                'Please update your blessclient by running `make update` '
                'in the bless folder.')

        raise LambdaInvocationException(
            'BLESS client did not recieve a valid cert. Instead got: {}'.format(cert))

    # Remove old certificate, replacing with new certificate
    ssh_agent_remove_bless(identity_file)
    with open(cert_file, 'w') as cert_file:
        cert_file.write(cert)
    ssh_agent_add_bless(identity_file)

    # bless_cache.set('certip', my_ip)
    # bless_cache.save()

    logging.debug("Successfully issued cert!")
    if show_feedback:
        sys.stderr.write("Finished getting certificate.\n")


def bless(region, nocache, showgui, hostname, bless_config, username=None):
    # Setup loggging
    setup_logging()
    show_feedback = get_stderr_feedback()
    logging.debug("Starting...")

    if os.getenv('MFA_ROLE', '') != '':
        awsmfautils.unset_token()

    aws = BlessAWS()
    bless_cache = get_bless_cache(nocache, bless_config)
    update_client(bless_cache, bless_config)
    bless_lambda_config = bless_config.get_lambda_config()

    userIP = UserIP(
        bless_cache=bless_cache,
        maxcachetime=bless_lambda_config['ipcachelifetime'],
        ip_urls=bless_config.get_client_config()['ip_urls'],
        fixed_ip=os.getenv('BLESSFIXEDIP', False))
    my_ip = userIP.getIP()

    if username is None:
        username = get_username(aws, bless_cache)

    clistring = psutil.Process(os.getppid()).cmdline()
    identity_file = get_idfile_from_cmdline(
        clistring,
        os.path.expanduser('~/.ssh/blessid'),
    )
    cert_file = identity_file + '-cert.pub'

    logging.debug("Using identity file: {}".format(identity_file))

    role_creds = None
    kmsauth_config = get_kmsauth_config(region, bless_config)
    client_config = bless_config.get_client_config()
    creds, role_creds, kmsauth_token = get_env_creds(aws, client_config, kmsauth_config, username, bless_cache, bless_config)

    if role_creds is None:
        sys.stderr.write('AWS session not working. Check blessclient.cfg and verify the aws session?\n')
        sys.exit(1)

    ip_list = None
    ip = None
    if get_housekeeper_config(region, bless_config) is None:
        ip = None
        if 'bastion_ips' in bless_config.get_aws_config():
            ip_list = "{},{}".format(my_ip, bless_config.get_aws_config()['bastion_ips'])
        else:
            ip_list = '{}'.format(my_ip)
    else:
        try:
            role_creds_hk = get_housekeeperrole_credentials(
                aws.iam_client(), creds, get_housekeeper_config(region, bless_config), bless_config, bless_cache)
            housekeeper = HousekeeperLambda(get_housekeeper_config(region, bless_config), role_creds_hk, region)
            if is_valid_ipv4_address(hostname):
                ip = hostname
            else:
                bastion_list = housekeeper.getPrivateIpFromPublicName(hostname)
                if bastion_list is not None:
                    bastion_list = ','.join(bastion_list)
                    ip_list = "{},{}".format(my_ip, bastion_list)
                else:
                    ip = socket.gethostbyname(hostname)
            if ip is not None and ip_list is None:
                if bless_cache.get('remote_ip') == ip:
                    ip_list = bless_cache.get('bastion_ips')
                else:
                    private_ip = housekeeper.getPrivateIpFromPublic(ip)
                    if private_ip is not None:
                        ip_list = "{},{}".format(my_ip, private_ip)
                    else:
                        if 'bastion_ips' in bless_config.get_aws_config():
                            ip_list = "{},{}".format(my_ip, bless_config.get_aws_config()['bastion_ips'])
                        else:
                            ip_list = '{}'.format(my_ip)
            elif ip_list is None:
                if 'bastion_ips' in bless_config.get_aws_config():
                    ip_list = "{},{}".format(my_ip, bless_config.get_aws_config()['bastion_ips'])
                else:
                    ip_list = '{}'.format(my_ip)
        except Exception as e:
            if 'bastion_ips' in bless_config.get_aws_config():
                ip_list = "{},{}".format(my_ip, bless_config.get_aws_config()['bastion_ips'])
            else:
                ip_list = '{}'.format(my_ip)
            raise e

    if nocache is not True:
        if check_fresh_cert(cert_file, bless_lambda_config, bless_cache, userIP, ip_list):
            logging.debug("Already have fresh cert")
            return {"username": username}

    bless_cache.set('bastion_ips', ip_list)
    bless_cache.set('remote_ip', ip)
    bless_cache.save()

    bless_lambda = BlessLambda(bless_lambda_config, role_creds, kmsauth_token, region)

    # Do bless
    if show_feedback:
        sys.stderr.write(
            "Requesting certificate for your public key"
            + " (set BLESSQUIET=1 to suppress these messages)\n"
        )
    public_key_file = identity_file + '.pub'
    try:
        with open(public_key_file, 'r') as f:
            public_key = f.read()
    except FileNotFoundError as e:
        generate_ssh_key(identity_file, public_key_file)
        with open(public_key_file, 'r') as f:
            public_key = f.read()

    if public_key[:8] != 'ssh-rsa ':
        raise Exception(
            'Refusing to bless {}. Probably not an identity file.'.format(identity_file))

    remote_user = bless_config.get_aws_config()['remote_user'] or username
    payload = {
        'bastion_user': username,
        'bastion_user_ip': my_ip,
        'remote_usernames': remote_user,
        'bastion_ips': ip_list,
        'command': '*',
        'public_key_to_sign': public_key,
    }
    cert = bless_lambda.getCert(payload)

    logging.debug("Got back cert: {}".format(cert))

    if cert[:29] != 'ssh-rsa-cert-v01@openssh.com ':
        error_msg = json.loads(cert)
        if ('errorType' in error_msg
            and error_msg['errorType'] == 'KMSAuthValidationError'
            and nocache is False
        ):
            logging.debug("KMSAuth error with cached token, purging cache.")
            clear_kmsauth_token_cache(kmsauth_config, bless_cache)
            raise LambdaInvocationException('KMSAuth validation error')

        if ('errorType' in error_msg and
                error_msg['errorType'] == 'ClientError'):
            raise LambdaInvocationException(
                'The BLESS lambda experienced a client error. Consider trying in a different region.'
            )

        if ('errorType' in error_msg and
                error_msg['errorType'] == 'InputValidationError'):
            raise Exception(
                'The input to the BLESS lambda is invalid. '
                'Please update your blessclient by running `make update` '
                'in the bless folder.')

        raise LambdaInvocationException(
            'BLESS client did not recieve a valid cert. Instead got: {}'.format(cert))

    # Remove RSA identity from ssh-agent (if it exists)
    ssh_agent_remove_bless(identity_file)
    with open(cert_file, 'w') as cert_file:
        cert_file.write(cert)

    # Check if we can skip adding identity into the running ssh-agent
    if bless_config.get_client_config()['update_sshagent'] is True:
        ssh_agent_add_bless(identity_file)
    else:
        logging.info(
            "Skipping loading identity into the running ssh-agent "
            'because this was disabled in the blessclient config.')

    bless_cache.set('certip', my_ip)
    bless_cache.save()

    logging.debug("Successfully issued cert!")
    if show_feedback:
        sys.stderr.write("Finished getting certificate.\n")

    return {"username": username}


def main():
    parser = argparse.ArgumentParser(
        description=('A client for getting BLESS\'ed ssh certificates.')
    )
    parser.add_argument(
        'host',
        help=(
            'Host name to which we are connecting'),
        nargs='*'
    )
    parser.add_argument(
        '--region',
        help=(
            'Region to which you want the lambda to connect to. Defaults to first region in config'),
        default=None
    )
    parser.add_argument(
        '--gui',
        help=(
            'If you need to input your AWS MFA token, use a gui (useful for interupting ssh)'),
        action='store_true'
    )
    parser.add_argument(
        '--config',
        help=(
            'Config file for blessclient, defaults to blessclient.cfg')
    )
    parser.add_argument(
        '--download_config',
        help=(
            'Download blessclient.cfg from S3 bucket. Will overwrite if file already exist'),
        action='store_true'
    )
    args = parser.parse_args()
    bless_config = BlessConfig()

    if len(args.host) == 0 and args.download_config is False:
        sys.stderr.write('blessclient: error: the following arguments are required: host\n')
        sys.exit(1)

    if load_config(bless_config, args.config, args.download_config) is False:
        sys.exit(1)

    if len(args.host) < 1:
        sys.exit(0)

    ca_backend = bless_config.get('BLESS_CONFIG')['ca_backend']
    if 'AWS_PROFILE' not in os.environ:
        sys.stderr.write('AWS session not found. Try running get_session first?\n')
        sys.exit(1)
    if re.match(bless_config.get_client_config()['domain_regex'], args.host[0]) or args.host[0] == 'BLESS':
        start_region = get_region_from_code(args.region, bless_config)
        success = False
        for region in get_regions(start_region, bless_config):
            try:
                if ca_backend.lower() == 'hashicorp-vault':
                    vault_bless(args.nocache, bless_config)
                    success = True
                elif ca_backend.lower() == 'bless':
                    bless(region, True, args.gui, args.host[0], bless_config)
                    success = True
                else:
                    sys.stderr.write('{0} is an invalid CA backend'.format(ca_backend))
                    sys.exit(1)
                break
            except ClientError as e:
                if e.response.get('Error', {}).get('Code') == 'InvalidSignatureException':
                    sys.stderr.write(
                        'Your authentication signature was rejected by AWS; try checking your system ' +
                        'date & timezone settings are correct\n')
                logging.info(
                    'Lambda execution error: {}. Trying again in the alternate region.'.format(str(e)))
            except (LambdaInvocationException, ConnectionError, EndpointConnectionError) as e:
                logging.info(
                    'Lambda execution error: {}. Trying again in the alternate region.'.format(str(e)))
        if success:
            sys.exit(0)
        else:
            sys.stderr.write('Could not sign SSH public key.\n')
            sys.exit(1)
    else:
        sys.exit(1)


if __name__ == '__main__':
    main()
