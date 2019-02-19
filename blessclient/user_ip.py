from __future__ import absolute_import
import contextlib
import logging
import string
import time
import socket
import requests
from urllib.parse import urlparse

VALID_IP_CHARACTERS = string.hexdigits + '.:'


class UserIP(object):

    def __init__(self, bless_cache, maxcachetime, ip_urls, fixed_ip=False):
        self.fresh = False
        self.currentIP = None
        self.cache = bless_cache
        self.maxcachetime = maxcachetime
        self.ip_urls = ip_urls
        if fixed_ip:
            self.currentIP = fixed_ip
            self.fresh = True

    def getIP(self):
        if self.fresh and self.currentIP:
            return self.currentIP
        lastip = self.cache.get('lastip')
        lastiptime = self.cache.get('lastipchecktime')
        if lastiptime and lastiptime + self.maxcachetime > time.time():
            return lastip
        self._refreshIP()
        return self.currentIP

    def _refreshIP(self):
        logging.debug("Getting current public IP")

        ip = None
        for url in self.ip_urls:
            if ip:
                break
            else:
                ip = self._fetchIP(url)

        if not ip:
            raise Exception('Could not refresh public IP')

        self.currentIP = ip
        self.fresh = True
        self.cache.set('lastip', self.currentIP)
        self.cache.set('lastipchecktime', time.time())
        self.cache.save()

    def _fetchIP(self, url):
        try:
            # We do this to force IPv4 lookup as bless do not currently support IPv6
            parsed_uri = urlparse(url)
            addrs = socket.gethostbyname(parsed_uri.netloc)
            headers = { 'Host' : parsed_uri.netloc }
            r = requests.get('{}://{}{}'.format(parsed_uri.scheme, addrs, parsed_uri.path), headers=headers)
            if r.status_code == 200:
                content = r.text.strip()
                for c in content:
                    if c not in VALID_IP_CHARACTERS:
                        print(content)
                        raise ValueError("Public IP response included invalid character '{}'.".format(c))
                logging.debug('Public IP is {}'.format(content))
                return content
        except Exception as e:
            logging.debug(e)
            logging.debug('Could not refresh public IP from {}'.format(url), exc_info=True)

        return None
