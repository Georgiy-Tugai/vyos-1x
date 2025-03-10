#!/usr/bin/env python3
#
# Copyright (C) 2018-2020 VyOS maintainers and contributors
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 2 or later as
# published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import os
from sys import exit

from vyos.config import Config
from vyos.configdict import dict_merge
from vyos.pki import wrap_certificate
from vyos.pki import wrap_private_key
from vyos.template import render
from vyos.util import call
from vyos.xml import defaults
from vyos import ConfigError
from crypt import crypt, mksalt, METHOD_SHA512

from vyos import airbag
airbag.enable()

cfg_dir        = '/run/ocserv'
ocserv_conf    = cfg_dir + '/ocserv.conf'
ocserv_passwd  = cfg_dir + '/ocpasswd'
radius_cfg     = cfg_dir + '/radiusclient.conf'
radius_servers = cfg_dir + '/radius_servers'

# Generate hash from user cleartext password
def get_hash(password):
    return crypt(password, mksalt(METHOD_SHA512))

def get_config():
    conf = Config()
    base = ['vpn', 'openconnect']
    if not conf.exists(base):
        return None

    ocserv = conf.get_config_dict(base, key_mangling=('-', '_'), get_first_key=True)
    # We have gathered the dict representation of the CLI, but there are default
    # options which we need to update into the dictionary retrived.
    default_values = defaults(base)
    ocserv = dict_merge(default_values, ocserv)

    if ocserv:
        ocserv['pki'] = conf.get_config_dict(['pki'], key_mangling=('-', '_'),
                                get_first_key=True, no_tag_node_value_mangle=True)

    return ocserv

def verify(ocserv):
    if ocserv is None:
        return None

    # Check authentication
    if "authentication" in ocserv:
        if "mode" in ocserv["authentication"]:
            if "local" in ocserv["authentication"]["mode"]:
                if not ocserv["authentication"]["local_users"] or not ocserv["authentication"]["local_users"]["username"]:
                    raise ConfigError('openconnect mode local required at leat one user')
                else:
                    for user in ocserv["authentication"]["local_users"]["username"]:
                        if not "password" in ocserv["authentication"]["local_users"]["username"][user]:
                            raise ConfigError(f'password required for user {user}')
        else:
            raise ConfigError('openconnect authentication mode required')
    else:
        raise ConfigError('openconnect authentication credentials required')

    # Check ssl
    if 'ssl' not in ocserv:
        raise ConfigError('openconnect ssl required')

    if not ocserv['pki'] or 'certificate' not in ocserv['pki']:
        raise ConfigError('PKI not configured')

    ssl = ocserv['ssl']
    if 'certificate' not in ssl:
        raise ConfigError('openconnect ssl certificate required')

    cert_name = ssl['certificate']

    if cert_name not in ocserv['pki']['certificate']:
        raise ConfigError('Invalid openconnect ssl certificate')

    cert = ocserv['pki']['certificate'][cert_name]

    if 'certificate' not in cert:
        raise ConfigError('Missing certificate in PKI')

    if 'private' not in cert or 'key' not in cert['private']:
        raise ConfigError('Missing private key in PKI')

    if 'ca_certificate' in ssl:
        if 'ca' not in ocserv['pki']:
            raise ConfigError('PKI not configured')

        if ssl['ca_certificate'] not in ocserv['pki']['ca']:
            raise ConfigError('Invalid openconnect ssl CA certificate')

    # Check network settings
    if "network_settings" in ocserv:
        if "push_route" in ocserv["network_settings"]:
            # Replace default route
            if "0.0.0.0/0" in ocserv["network_settings"]["push_route"]:
                ocserv["network_settings"]["push_route"].remove("0.0.0.0/0")
                ocserv["network_settings"]["push_route"].append("default")
        else:
            ocserv["network_settings"]["push_route"] = "default"
    else:
        raise ConfigError('openconnect network settings required')


def generate(ocserv):
    if not ocserv:
        return None

    if "radius" in ocserv["authentication"]["mode"]:
        # Render radius client configuration
        render(radius_cfg, 'ocserv/radius_conf.tmpl', ocserv["authentication"]["radius"])
        # Render radius servers
        render(radius_servers, 'ocserv/radius_servers.tmpl', ocserv["authentication"]["radius"])
    else:
        if "local_users" in ocserv["authentication"]:
            for user in ocserv["authentication"]["local_users"]["username"]:
                ocserv["authentication"]["local_users"]["username"][user]["hash"] = get_hash(ocserv["authentication"]["local_users"]["username"][user]["password"])
            # Render local users
            render(ocserv_passwd, 'ocserv/ocserv_passwd.tmpl', ocserv["authentication"]["local_users"])

    if "ssl" in ocserv:
        cert_file_path = os.path.join(cfg_dir, 'cert.pem')
        cert_key_path = os.path.join(cfg_dir, 'cert.key')
        ca_cert_file_path = os.path.join(cfg_dir, 'ca.pem')

        if 'certificate' in ocserv['ssl']:
            cert_name = ocserv['ssl']['certificate']
            pki_cert = ocserv['pki']['certificate'][cert_name]

            with open(cert_file_path, 'w') as f:
                f.write(wrap_certificate(pki_cert['certificate']))

            if 'private' in pki_cert and 'key' in pki_cert['private']:
                with open(cert_key_path, 'w') as f:
                    f.write(wrap_private_key(pki_cert['private']['key']))

        if 'ca_certificate' in ocserv['ssl']:
            ca_name = ocserv['ssl']['ca_certificate']
            pki_ca_cert = ocserv['pki']['ca'][ca_name]

            with open(ca_cert_file_path, 'w') as f:
                f.write(wrap_certificate(pki_ca_cert['certificate']))

    # Render config
    render(ocserv_conf, 'ocserv/ocserv_config.tmpl', ocserv)


def apply(ocserv):
    if not ocserv:
        call('systemctl stop ocserv.service')
        for file in [ocserv_conf, ocserv_passwd]:
            if os.path.exists(file):
                os.unlink(file)
    else:
        call('systemctl restart ocserv.service')


if __name__ == '__main__':
    try:
        c = get_config()
        verify(c)
        generate(c)
        apply(c)
    except ConfigError as e:
        print(e)
        exit(1)
