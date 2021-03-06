#!/usr/bin/env python
#
# Copyright 2016 Brocade Communications Systems, Inc.  All rights reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.
#
# Matthew Geldert (mgeldert@brocade.com), Brocade Communications Systems,Inc.
#

import base64
import json
from neutronclient.neutron import client as neutron_client
from oslo_log import log as logging
from oslo_config import cfg
from random import choice, randint
import re
import requests
from requests.packages.urllib3.exceptions import InsecureRequestWarning
import socket
from string import ascii_letters, digits
from struct import pack
from time import sleep

LOG = logging.getLogger(__name__)
requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

class OpenStackInterface(object):
    def __init__(self):
        self.admin_username = cfg.CONF.lbaas_settings.openstack_username
        self.admin_password = cfg.CONF.lbaas_settings.openstack_password
        # Get Neutron and Nova API endpoints...
        keystone = self.get_keystone_client()
        neutron_service = keystone.services.find(name="neutron")
        nova_service = keystone.services.find(name="nova")
        if cfg.CONF.lbaas_settings.keystone_version == "2":
            self.neutron_endpoint = keystone.endpoints.find(
                service_id=neutron_service.id
            ).adminurl
            self.nova_endpoint = keystone.endpoints.find(
                service_id=nova_service.id
            ).adminurl
        else:
            self.neutron_endpoint = keystone.endpoints.find(
                interface="admin", service_id=neutron_service.id
            ).url
            self.nova_endpoint = keystone.endpoints.find(
                interface="admin", service_id=nova_service.id
            ).url

    def create_vtm(self, hostname, lb):
        """
        Creates a vTM instance as a Nova VM.
        """
        password = self._generate_password()
        net_info = self._configure_ports(lb, hostname)
        # Get user-data to pass to the configuration scripts in the
        # vTM image
        user_data = self._generate_user_data(
            hostname, password, net_info['data_port'], net_info['mgmt_port']
        )
        cloud_init_file = self._generate_cloud_init_file(user_data)
        self.boot_vtm(lb.tenant_id, hostname, cloud_init_file,
                      net_info['nics'], password)
        return (net_info['mgmt_ip'], password)

    def create_vtms(self, hostnames, lb):
        """
        Creates an HA cluster of vTM instances.
        """
        password = self._generate_password()
        # Configure ports and security groups for both instances
        primary_net_info = self._configure_ports(
            lb, hostnames[0], cluster=True
        )
        security_groups = {
            "ports": primary_net_info['ports_sec_grp'],
            "mgmt": primary_net_info['mgmt_sec_grp']
        }
        secondary_net_info = self._configure_ports(
            lb, hostnames[1], security_groups, True
        )
        # Create primary instance
        primary_user_data = self._generate_user_data(
            hostnames[0], password,
            primary_net_info['data_port'],
            primary_net_info['mgmt_port'],
            {
                "is_primary": True,
                "peer_name": hostnames[1],
                "peer_addr": secondary_net_info['cluster_addr']
            }
        )
        cloud_init_file = self._generate_cloud_init_file(primary_user_data)
        self.boot_vtm(
            lb.tenant_id, hostnames[0],
            cloud_init_file, primary_net_info['nics'], password
        )
        # Give the primary chance to come up and stabilize
        # before the second instance tries to cluster to it
        sleep(10)
        # Create secondary instance
        secondary_user_data = self._generate_user_data(
            hostnames[1], password,
            secondary_net_info['data_port'],
            secondary_net_info['mgmt_port'],
            {
                "is_primary": False,
                "peer_name": hostnames[0],
                "peer_addr": primary_net_info['cluster_addr']
            }
        )
        cloud_init_file = self._generate_cloud_init_file(secondary_user_data)
        self.boot_vtm(
            lb.tenant_id, hostnames[1],
            cloud_init_file, secondary_net_info['nics'], password
        )
        return {
            "password": password,
            "nodes": [
                {
                    "hostname": hostnames[0],
                    "mgmt_ip": primary_net_info['mgmt_ip']
                },
                {
                    "hostname": hostnames[1],
                    "mgmt_ip": secondary_net_info['mgmt_ip']
                }
            ]
        }

    def boot_vtm(self, tenant_id, hostname, user_data, nics, password):
        """
        Boots a vTM instance.
        """
        instance = self.create_server(
            tenant_id=tenant_id,
            hostname=hostname,
            user_data=user_data,
            nics=nics,
            password=password
        )
        self.set_server_lock(tenant_id, instance['server']['id'], lock=True)
        self._await_build_complete(tenant_id, instance['server']['id'])
        return instance

    def destroy_vtm(self, hostname, lb):
        port_list = []
        sec_grp_list = []
        floatingip_list = []
        server_id = self.get_server_id_from_hostname(lb.tenant_id, hostname)
        neutron = self.get_neutron_client()
        # Build lists of ports, floating IPs and security groups to delete
        ports = neutron.list_ports(device_id=server_id)
        for port in ports['ports']:
            port_list.append(port['id'])
            sec_grp_list += port['security_groups']
            floatingip_list += [
                floatingip['id']
                for floatingip in neutron.list_floatingips(
                    port_id=port['id']
                )['floatingips']
            ]
        # Delete the instance
        self.delete_server(lb.tenant_id, server_id)
        # Delete floating IPs
        for flip in floatingip_list:
            try:
                neutron.delete_floatingip(flip)
            except Exception as e:
                LOG.error(_("\nError deleting floating IP %s: %s" % (flip, e)))
        # Delete ports
        for port in port_list:
            if port != lb.vip_port_id:
                # Port isn't bound to the LBaaS "loadbalancer" object so
                # just delete it.
                neutron.delete_port(port)
            else:
                # Port can't be deleted as it's still bound to the LBaaS
                # "loadbalancer" object. Therefore just set the security
                # group to nothing so it too isn't bound, and can be deleted.
                neutron.update_port(
                    port,
                    {"port": {"security_groups": []}}
                )
        # Delete security groups
        for sec_grp in sec_grp_list:
            try:
                neutron.delete_security_group(sec_grp)
            except Exception:
                # Might legitimately fail in HA deployments
                pass

    def vtm_exists(self, tenant_id, hostname):
        """
        Tests whether a vTM instance with the specified hosname exists.
        """
        hostname = hostname[0] if isinstance(hostname, tuple) else hostname
        try:
            self.get_server_id_from_hostname(tenant_id, hostname)
            return True
        except:
            return False

    def add_ip_to_ports(self, ip, ports):
        """
        Adds IP address to the allowed_address_pairs field of ports.
        """
        neutron = self.get_neutron_client()
        # Loop through all the ports, typically one per vTM cluster member
        for port_id in ports:
            port = neutron.show_port(port_id)['port']
            port_ips = [
                addr['ip_address'] for addr in port['allowed_address_pairs']
            ]
            # Add the IP if it isn't already in allowed_address_pairs
            if ip not in port_ips:
                port_ips.append(ip)
                allowed_pairs = []
                for addr in port_ips:
                    allowed_pairs.append({"ip_address": addr})
                neutron.update_port(
                    port_id, {"port": {
                        "allowed_address_pairs": allowed_pairs
                    }}
                )

    def delete_ip_from_ports(self, ip, ports):
        """
        Deletes IP address from the allowed_address_pairs field of ports.
        """
        neutron = self.get_neutron_client()
        # Loop through all the ports, typically one per vTM cluster member
        for port_id in ports:
            port = neutron.show_port(port_id)['port']
            port_ips = [
                addr['ip_address'] for addr in port['allowed_address_pairs']
            ]
            # Delete the IP if it is in allowed_address_pairs
            if ip in port_ips:
                new_pairs = []
                for port_ip in port_ips:
                    if ip != port_ip:
                        new_pairs.append({"ip_address": port_ip})
                neutron.update_port(
                    port_id, {"port": {"allowed_address_pairs": new_pairs}}
                )

    def _await_build_complete(self, tenant_id, instance_id):
        """
        Waits for a Nova instance to be built.
        """
        instance = self.get_server(tenant_id, instance_id)
        status = instance['server']['status']
        while status == 'BUILD':
            sleep(10)
            instance = self.get_server(tenant_id, instance_id)
            status = instance['server']['status']
            if status == 'ERROR':
                self.delete_server(tenant_id, instance_id)
                raise Exception("VM build failed")

    def _configure_ports(self, lb, hostname, security_groups=None, cluster=False):
        neutron = self.get_neutron_client()
        # A new port, not tied to a "loadbalancer", is needed as the
        # instance's main address (so it isn't deleted when the "lb" is).
        network_id = neutron.show_subnet(
            lb.vip_subnet_id
        )['subnet']['network_id']
        data_port = neutron.create_port(
            {"port": {
                "network_id": network_id,
                "tenant_id": lb.tenant_id,
                "name": "data-%s" % hostname
            }}
        )['port']
        if cfg.CONF.lbaas_settings.deployment_model == "PER_LOADBALANCER":
            sec_grp_uuid = lb.id
        elif cfg.CONF.lbaas_settings.deployment_model == "PER_TENANT":
            sec_grp_uuid = lb.tenant_id

        # Configure specified management method...
        data = {
            "data_port": data_port
        }
        if cfg.CONF.lbaas_settings.management_mode == "FLOATING_IP":
            # Create floating IP for management traffic
            floatingip = self.create_floatingip(lb.tenant_id, data_port['id'])
            if security_groups is None:
                # Create security group and add rules for management traffic
                sec_grp = self.create_lb_security_group(
                    lb.tenant_id, sec_grp_uuid, mgmt_port=True, cluster=cluster
                )
                sec_grp_id = sec_grp['security_group']['id']
            else:
                # Use an existing security group
                sec_grp_id = security_groups['ports']
            neutron.update_port(
                data_port['id'],
                {"port": {
                    "security_groups": [sec_grp_id],
                    "admin_state_up": True
                }}
            )
            # Set return data
            if cluster:
                data['cluster_addr'] = data_port['fixed_ips'][0]['ip_address']
            else:
                data['cluster_addr'] = None
            data['nics'] = [{"port": data_port['id']}]
            data['ports_sec_grp'] = sec_grp_id
            data['mgmt_sec_grp'] = None
            data['mgmt_ip'] = floatingip['floatingip']['floating_ip_address']
            data['mgmt_port'] = None
        elif cfg.CONF.lbaas_settings.management_mode == "MGMT_NET":
            if security_groups is None:
                # Create a security groups for the service and management ports
                ports_sec_grp = self.create_lb_security_group(
                    lb.tenant_id, sec_grp_uuid
                )
                mgmt_sec_grp = self.create_lb_security_group(
                    lb.tenant_id, sec_grp_uuid, mgmt_port=True,
                    mgmt_label=True, cluster=cluster
                )
                ports_sec_grp_id = ports_sec_grp['security_group']['id']
                mgmt_sec_grp_id = mgmt_sec_grp['security_group']['id']
            else:
                # Use existing security groups
                ports_sec_grp_id = security_groups['ports']
                mgmt_sec_grp_id = security_groups['mgmt']
            # Update data port with security group
            neutron.update_port(
                data_port['id'],
                {"port": {
                    "security_groups": [ports_sec_grp_id],
                    "admin_state_up": True
                }}
            )
            # Create the management port
            mgmt_port = self.create_mgmt_port(
                lb.tenant_id, hostname, mgmt_sec_grp_id
            )['port']
            # Set return data
            if cluster:
                data['cluster_addr'] = mgmt_port['fixed_ips'][0]['ip_address']
            else:
                data['cluster_addr'] = None
            data['nics'] = [
                {"port": mgmt_port['id']},
                {"port": data_port['id']}
            ]
            data['ports_sec_grp'] = ports_sec_grp_id
            data['mgmt_sec_grp'] = mgmt_sec_grp_id
            data['mgmt_ip'] = mgmt_port['fixed_ips'][0]['ip_address']
            data['mgmt_port'] = mgmt_port
        return data

    def create_lb_security_group(self, tenant_id, uuid, mgmt_port=False,
                                 mgmt_label=False, cluster=False):
        """
        Creates a security group.
        """
        neutron = self.get_neutron_client()
        sec_grp_data = {"security_group": {
            "name": "%slbaas-%s" % ("mgmt-" if mgmt_label else "", uuid),
            "tenant_id": tenant_id
        }}
        sec_grp = neutron.create_security_group(sec_grp_data)
        # If GUI access is allowed, open up the GUI port
        if cfg.CONF.vtm_settings.gui_access is True and not mgmt_label:
            self.create_security_group_rule(
                sec_grp['security_group']['id'],
                port=cfg.CONF.vtm_settings.admin_port
            )
        # If mgmt_port, add the necessary rules to allow management traffic
        # i.e. allow each Services Director to access the REST port of the
        # instance
        if mgmt_port:
            for server in cfg.CONF.lbaas_settings.admin_servers:
                self.create_security_group_rule(
                    sec_grp['security_group']['id'],
                    port=cfg.CONF.vtm_settings.rest_port,
                    src_addr=socket.gethostbyname(server)
                )
            if cfg.CONF.lbaas_settings.neutron_servers is not None:
                for server in cfg.CONF.lbaas_settings.neutron_servers:
                    self.create_security_group_rule(
                        sec_grp['security_group']['id'],
                        port=cfg.CONF.vtm_settings.rest_port,
                        src_addr=socket.gethostbyname(server)
                    )
        # If cluster, add necessary ports for intra-cluster comms
        if cluster is True:
            self.create_security_group_rule(
                sec_grp['security_group']['id'],
                port=cfg.CONF.vtm_settings.admin_port,
                remote_group=sec_grp['security_group']['id']
            )
            self.create_security_group_rule(
                sec_grp['security_group']['id'],
                port=cfg.CONF.vtm_settings.admin_port,
                remote_group=sec_grp['security_group']['id'],
                protocol='udp'
            )
            self.create_security_group_rule(
                sec_grp['security_group']['id'],
                port=cfg.CONF.vtm_settings.cluster_port,
                remote_group=sec_grp['security_group']['id']
            )
            self.create_security_group_rule(
                sec_grp['security_group']['id'],
                port=cfg.CONF.vtm_settings.cluster_port,
                remote_group=sec_grp['security_group']['id'],
                protocol='udp'
            )
        return sec_grp

    def create_security_group_rule(self, sec_grp_id, port, src_addr=None,
                                   remote_group=None, direction="ingress",
                                   protocol='tcp'):
        """
        Creates the designatted rule in a security group.
        """
        neutron = self.get_neutron_client()
        new_rule = {"security_group_rule": {
            "direction": direction,
            "port_range_min": port,
            "ethertype": "IPv4",
            "port_range_max": port,
            "protocol": protocol,
            "security_group_id": sec_grp_id
        }}
        if src_addr:
            new_rule['security_group_rule']['remote_ip_prefix'] = src_addr
        if remote_group:
            new_rule['security_group_rule']['remote_group_id'] = remote_group
        try:
            neutron.create_security_group_rule(new_rule)
        except Exception:
            # Rule already exists
            pass

    def allow_port(self, lb, port, protocol='tcp'):
        """
        Adds access to a given port to a security group.
        """
        # Get the name of the security group for the "loadbalancer"
        if cfg.CONF.lbaas_settings.deployment_model == "PER_LOADBALANCER":
            sec_grp_name = "lbaas-%s" % lb.id
        elif cfg.CONF.lbaas_settings.deployment_model == "PER_TENANT":
            sec_grp_name = "lbaas-%s" % lb.tenant_id
        # Get the security group
        neutron = self.get_neutron_client()
        sec_grp = neutron.list_security_groups(
            name=sec_grp_name
        )['security_groups'][0]
        # Create the required rule
        self.create_security_group_rule(sec_grp['id'], port, protocol=protocol)

    def block_port(self, lb, port, protocol='tcp'):
        """
        Removes access to a given port from a security group.
        """
        # Get the name of the security group for the "loadbalancer"
        if cfg.CONF.lbaas_settings.deployment_model == "PER_LOADBALANCER":
            sec_grp_name = "lbaas-%s" % lb.id
        elif cfg.CONF.lbaas_settings.deployment_model == "PER_TENANT":
            sec_grp_name = "lbaas-%s" % lb.tenant_id
        # Get the security group
        neutron = self.get_neutron_client()
        sec_grp = neutron.list_security_groups(
            name=sec_grp_name
        )['security_groups'][0]
        # Iterate through all rules in the group and delete the matching one
        for rule in sec_grp['security_group_rules']:
            if rule['port_range_min'] == port \
                and rule['port_range_max'] == port \
                and rule['direction'] == "ingress" \
                and rule['protocol'] == protocol:
                neutron.delete_security_group_rule(rule['id'])
                break

    def create_mgmt_port(self, tenant_id, hostname, mgmt_sec_grp):
        """
        Creates a port for management traffic.
        """
        mgmt_net_id = cfg.CONF.lbaas_settings.management_network
        neutron = self.get_neutron_client()
        port = {
            "port": {
                "admin_state_up": True,
                "name": "mgmt-%s" % hostname,
                "network_id": mgmt_net_id,
                "security_groups": [mgmt_sec_grp],
                "tenant_id": tenant_id
            }
        }
        mgmt_port = neutron.create_port(port)
        return mgmt_port

    def create_floatingip(self, tenant_id, port_id):
        neutron = self.get_neutron_client()
        network = cfg.CONF.lbaas_settings.management_network
        floatingip_data = {"floatingip": {
            "floating_network_id": network,
            "port_id": port_id,
            "tenant_id": tenant_id
        }}
        return neutron.create_floatingip(floatingip_data)

    def get_network_for_subnet(self, subnet_id):
        neutron = self.get_neutron_client()
        return neutron.show_subnet(subnet_id)['subnet']['network_id']

    def create_server(self, tenant_id, hostname, user_data, nics, password):
        """
        Creates a Nova instance of the vTM image.
        """
        token = self.get_auth_token(tenant_id=tenant_id)
        headers = {
            "Content-Type": "application/json",
            "X-Auth-Token": token
        }
        body = {"server": {
            "imageRef": cfg.CONF.lbaas_settings.image_id,
            "flavorRef": cfg.CONF.lbaas_settings.flavor_id,
            "name": hostname,
            "user_data": base64.b64encode(user_data),
            "adminPass": password,
            "networks": nics,
            "config_drive": True
        }}
        try:
            endpoint = self.nova_endpoint.replace("$(tenant_id)s", tenant_id)
            endpoint = endpoint.replace("%(tenant_id)s", tenant_id)
            response = requests.post(
                "%s/servers" % endpoint,
                data=json.dumps(body),
                headers=headers
            )
        except Exception as e:
            LOG.error(_("\nError creating vTM instance: %s" % e))
        return response.json()

    def get_server(self, tenant_id, server_id):
        token = self.get_auth_token(tenant_id=tenant_id)
        endpoint = self.nova_endpoint.replace("$(tenant_id)s", tenant_id)
        endpoint = endpoint.replace("%(tenant_id)s", tenant_id)
        response = requests.get(
            "%s/servers/%s" % (endpoint, server_id),
            headers={"X-Auth-Token": token}
        )
        if response.status_code != 200:
            raise Exception("Server Not found")
        return response.json()

    def set_server_lock(self, tenant_id, server_id, lock=True):
        token = self.get_auth_token(tenant_id=tenant_id)
        endpoint = self.nova_endpoint.replace("$(tenant_id)s", tenant_id)
        endpoint = endpoint.replace("%(tenant_id)s", tenant_id)
        response = requests.post(
            "%s/servers/%s/action" % (endpoint, server_id),
            headers={
                "X-Auth-Token": token,
                "Content-Type": "application/json"
            },
            data='{ "%s": null }' % ("lock" if lock else "unlock")
        )
        if response.status_code != 202:
            raise Exception("Failed to lock server %s" % server_id)

    def get_server_port(self, tenant_id, hostname):
        """
        Gets the Neutron ID of a vTM's data ort.
        """
        neutron = self.get_neutron_client()
        server_id = self.get_server_id_from_hostname(tenant_id, hostname)
        ports = neutron.list_ports(device_id=server_id)['ports']
        for port in ports:
            if not port['name'].startswith("mgmt"):
                return port['id']
        raise Exception("No data port found for %s" % hostname)

    def get_server_id_from_hostname(self, tenant_id, hostname):
        """
        Gets the Nova ID of a server from its hostname.
        """
        token = self.get_auth_token(tenant_id=tenant_id)
        endpoint = self.nova_endpoint.replace("$(tenant_id)s", tenant_id)
        endpoint = endpoint.replace("%(tenant_id)s", tenant_id)
        response = requests.get(
            "%s/servers" % endpoint,
            headers={"X-Auth-Token": token}
        )
        for server in response.json()['servers']:
            if server['name'] == hostname:
                return server['id']
        raise Exception("Server not found")

    def delete_server(self, tenant_id, server_id):
        """
        Deletes a Nova instance.
        """
        self.set_server_lock(tenant_id, server_id, lock=False)
        token = self.get_auth_token(tenant_id=tenant_id)
        endpoint = self.nova_endpoint.replace("$(tenant_id)s", tenant_id)
        endpoint = endpoint.replace("%(tenant_id)s", tenant_id)
        requests.delete(
            "%s/servers/%s" % (endpoint, server_id),
            headers={"X-Auth-Token": token}
        )

    def get_neutron_client(self):
        auth_token = self.get_auth_token()
        neutron = neutron_client.Client(
            '2.0', endpoint_url=self.neutron_endpoint, token=auth_token
        )
        neutron.format = 'json'
        return neutron

    def get_keystone_client(self, tenant_id=None, tenant_name=None):
        auth_url = re.match(
            "^(https?://[^/]+)",
            cfg.CONF.keystone_authtoken.auth_uri
        ).group(1)
        if cfg.CONF.lbaas_settings.keystone_version == "2":
            from keystoneclient.v2_0 import client as keystone_client
            auth_url = "%s/v2.0" % auth_url
        else:
            from keystoneclient.v3 import client as keystone_client
            auth_url = "%s/v3" % auth_url
        param = {}
        if tenant_id:
            param['tenant_id'] = tenant_id
        elif tenant_name:
            param['tenant_name'] = tenant_name
        else:
            param['tenant_name'] = "admin"
        return keystone_client.Client(
            username=self.admin_username,
            password=self.admin_password,
            auth_url=auth_url,
            **param
        )

    def get_auth_token(self, tenant_id=None, tenant_name=None):
        keystone_client = self.get_keystone_client(tenant_id, tenant_name)
        return keystone_client.auth_token

    def get_netmask(self, cidr):
        mask = int(cidr.split("/")[1])
        bits = 0xffffffff ^ (1 << 32 - mask) - 1
        return socket.inet_ntoa(pack('>I', bits))

    def _generate_password(self):
        chars = ascii_letters + digits
        return "".join(choice(chars) for _ in range(randint(12, 16)))

    def _generate_user_data(self, hostname, password, data_port, mgmt_port,
                            cluster_data=None):
        neutron = self.get_neutron_client()
        data_subnet = neutron.show_subnet(
            data_port['fixed_ips'][0]['subnet_id']
        )['subnet']
        # Get bind IP for management services
        if mgmt_port:
            bind_ip = mgmt_port['fixed_ips'][0]['ip_address']
        else:
            bind_ip = data_port['fixed_ips'][0]['ip_address']
        # Build the replay data to feed into the config script on the instance
        replay_data = {
            "developer_mode_accepted": "Yes",
            "admin!password": password,
            "rest!enabled": "Yes",
            "appliance!timezone": cfg.CONF.vtm_settings.timezone,
            "appliance!hostname": hostname,
            "appliance!licence_agreed": "Yes",
            "rest!port": cfg.CONF.vtm_settings.rest_port,
            "appliance!gateway": data_subnet['gateway_ip'],
            "appliance!if!eth0!autoneg": "Yes",
            "appliance!if!eth0!mtu": cfg.CONF.vtm_settings.mtu,
            "appliance!ip!eth0!isexternal": "No",
            "rest!bindips": bind_ip,
            "control!bindip": bind_ip if cluster_data else "127.0.0.1",
            "appliance!nameservers":
                " ".join(cfg.CONF.vtm_settings.nameservers)
        }
        if cfg.CONF.vtm_settings.gui_access is True:
            replay_data['monitor_user'] = "monitor %s" % "password"
        else:
            replay_data.update({
                "access": " ".join(list(set([
                    socket.gethostbyname(server)
                    for server in cfg.CONF.lbaas_settings.admin_servers
                ])))
            })
            if cluster_data:
                replay_data['access'] += " %s" % cluster_data['peer_addr']
        if mgmt_port:
            mgmt_subnet = neutron.show_subnet(
                mgmt_port['fixed_ips'][0]['subnet_id']
            )['subnet']
            replay_data["appliance!hosts!%s" % hostname] = \
                mgmt_port['fixed_ips'][0]['ip_address']
            replay_data["appliance!ip!eth0!addr"] = \
                mgmt_port['fixed_ips'][0]['ip_address']
            mgmt_subnet = neutron.show_subnet(
                mgmt_port['fixed_ips'][0]['subnet_id']
            )['subnet']
            replay_data["appliance!ip!eth0!mask"] = self.get_netmask(
                mgmt_subnet['cidr']
            )
            replay_data["appliance!if!eth1!autoneg"] = "Yes"
            replay_data["appliance!if!eth1!mtu"] = "1454"
            replay_data["appliance!ip!eth1!isexternal"] = "No"
            replay_data["appliance!ip!eth1!addr"] = \
                data_port['fixed_ips'][0]['ip_address']
            replay_data["appliance!ip!eth1!mask"] = self.get_netmask(
                data_subnet['cidr']
            )
        else:
            replay_data["appliance!hosts!%s" % hostname] = \
                data_port['fixed_ips'][0]['ip_address']
            replay_data["appliance!ip!eth0!addr"] = \
                data_port['fixed_ips'][0]['ip_address']
            replay_data["appliance!ip!eth0!mask"] = self.get_netmask(
                data_subnet['cidr']
            )
        if cluster_data:
            replay_data.update({
                "appliance!hosts!%s" % cluster_data['peer_name']:
                cluster_data['peer_addr'],
                "controlallow": "localhost,%s,%s" % (
                    bind_ip, cluster_data['peer_addr']
                )
            })
            if cluster_data['is_primary'] is False:
                cluster_join_data = {
                    "accept-license": "accept",
                    "start_at_boot": "y",
                    "zxtm!group": "nogroup",
                    "zxtm!license_key": "",
                    "zxtm!name_useip": "n",
                    "zxtm!use_invalid_key_license": "y",
                    "zxtm!user": "nobody",
                    "zxtm!cluster": "S",
                    "zxtm!clustertipjoin": "y",
                    "zxtm!fingerprints_ok": "y",
                    "zlb!admin_username": cfg.CONF.vtm_settings.username,
                    "zlb!admin_port": cfg.CONF.vtm_settings.admin_port,
                    "zlb!admin_hostname": cluster_data['peer_addr'],
                    "zlb!admin_password": password,
                    "zxtm!join_new_cluster": "y",
                    "zxtm!name_setname": "1",
                    "zxtm!reconfigure_option": "2"
                }
                if cfg.CONF.vtm_settings.gui_access is True:
                    cluster_join_data['zxtm!unique_bind'] = "n"
                else:
                    cluster_join_data['zxtm!unique_bind'] = "n"
                    cluster_join_data['zxtm!bindip'] = bind_ip
                cluster_join_text = "\n".join([
                    "%s=%s" % (k, v) for k, v in cluster_join_data.iteritems()
                ])
            elif cfg.CONF.vtm_settings.gui_access is not True:
                cluster_join_data = {
                    "accept-license": "accept",
                    "start_at_boot": "y",
                    "zxtm!group": "nogroup",
                    "zxtm!license_key": "",
                    "zxtm!name_useip": "n",
                    "zxtm!unique_bind": "y",
                    "zxtm!use_invalid_key_license": "y",
                    "zxtm!user": "nobody",
                    "zxtm!cluster": "C",
                    "zxtm!bindip": bind_ip,
                    "zxtm!join_new_cluster": "n",
                    "zxtm!name_setname": "1",
                    "zxtm!reconfigure_option": "2"
                }
                cluster_join_text = "\n".join([
                    "%s=%s" % (k, v) for k, v in cluster_join_data.iteritems()
                ])
            else:
                cluster_join_text = None
        else:
            cluster_join_text = None
        replay_text = "\n".join(
            ["%s\t%s" % (k, v) for k, v in replay_data.iteritems()]
        )
        return {
            "replay_data": replay_text,
            "cluster_join_data": cluster_join_text,
            "password": password,
            "hostname": hostname
        }

    def _generate_cloud_init_file(self, user_data):
        return ("""#cloud-config
write_files:
-   encoding: b64
    content: %s
    path: /root/config_data

-   encoding: b64
    content: """
"IyEvdXNyL2Jpbi9lbnYgcHl0aG9uCiNDb3B5cmlnaHQgMjAxNCBCcm9jYWRlIENvbW11bmljYXRpb"
"25zIFN5c3RlbXMsIEluYy4gIEFsbCByaWdodHMgcmVzZXJ2ZWQuCgppbXBvcnQgb3MKaW1wb3J0IG"
"pzb24KZnJvbSBzdWJwcm9jZXNzIGltcG9ydCBQb3BlbiwgUElQRSwgU1RET1VULCBjYWxsCgpjbGF"
"zcyBDb25maWdGaWxlKGRpY3QpOgogICAgZGVmIF9faW5pdF9fKHNlbGYsIG5hbWUsIHBhdGgpOgog"
"ICAgICAgIHNlbGYuZmlsZW5hbWUgPSAiJXMvJXMiICUgKHBhdGgsIG5hbWUpCiAgICAgICAgc2VsZ"
"i5fZ2V0X2N1cnJlbnRfa2V5cygpCgogICAgZGVmIGFwcGx5KHNlbGYpOgogICAgICAgIHdpdGggb3"
"BlbihzZWxmLmZpbGVuYW1lLCAidyIpIGFzIGNvbmZpZ19maWxlOgogICAgICAgICAgICBmb3Iga2V"
"5LCB2YWx1ZSBpbiBzZWxmLml0ZXJpdGVtcygpOgogICAgICAgICAgICAgICAgY29uZmlnX2ZpbGUu"
"d3JpdGUoIiVzXHQlc1xuIiAlIChrZXksIHZhbHVlKSkKCiAgICBkZWYgX2dldF9jdXJyZW50X2tle"
"XMoc2VsZik6CiAgICAgICAgd2l0aCBvcGVuKHNlbGYuZmlsZW5hbWUpIGFzIGNvbmZpZ19maWxlOg"
"ogICAgICAgICAgICBmb3IgbGluZSBpbiBjb25maWdfZmlsZToKICAgICAgICAgICAgICAgIHRyeTo"
"KICAgICAgICAgICAgICAgICAgICBiaXRzID0gbGluZS5zcGxpdCgpCiAgICAgICAgICAgICAgICAg"
"ICAgc2VsZltiaXRzWzBdXSA9ICIgIi5qb2luKGJpdHNbMTpdKQogICAgICAgICAgICAgICAgZXhjZ"
"XB0OgogICAgICAgICAgICAgICAgICAgIHBhc3MKICAgICAgICAKCmNsYXNzIFJlcGxheURhdGEoZG"
"ljdCk6CiAgICBjbGFzcyBSZXBsYXlEYXRhUGFyYW1ldGVyKG9iamVjdCk6CiAgICAgICAgZGVmIF9"
"faW5pdF9fKHNlbGYsIHRleHQpOgogICAgICAgICAgICB3b3JkcyA9IHRleHQuc3RyaXAoKS5zcGxp"
"dCgpCiAgICAgICAgICAgIHNlbGYua2V5ID0gd29yZHNbMF0KICAgICAgICAgICAgc2VsZi5wcmVma"
"XggPSB3b3Jkc1swXS5zcGxpdCgiISIpWzBdCiAgICAgICAgICAgIHNlbGYudmFsdWVfbGlzdCA9IH"
"dvcmRzWzE6XQogICAgICAgICAgICBzZWxmLnZhbHVlX3N0ciA9ICIgIi5qb2luKHdvcmRzWzE6XSk"
"KCiAgICBkZWYgX19pbml0X18oc2VsZiwgdGV4dCk6CiAgICAgICAgZm9yIGxpbmUgaW4gdGV4dC5z"
"cGxpdCgiXG4iKToKICAgICAgICAgICAgd29yZHMgPSBsaW5lLnNwbGl0KCkKICAgICAgICAgICAgd"
"HJ5OgogICAgICAgICAgICAgICAgc2VsZlt3b3Jkc1swXV0gPSBzZWxmLlJlcGxheURhdGFQYXJhbW"
"V0ZXIobGluZSkKICAgICAgICAgICAgZXhjZXB0IEluZGV4RXJyb3I6CiAgICAgICAgICAgICAgICB"
"wYXNzCiAgICAgICAgCgpkZWYgbWFpbigpOgogICAgWkVVU0hPTUUgPSBvcy5lbnZpcm9uLmdldCgn"
"WkVVU0hPTUUnLCAnL29wdC96ZXVzJykKICAgIG5ld191c2VyID0gTm9uZQogICAgdXVpZF9nZW5lc"
"mF0ZV9wcm9jID0gUG9wZW4oCiAgICAgICAgWyIlcy96eHRtL2Jpbi96Y2xpIiAlIFpFVVNIT01FXS"
"wKICAgICAgICBzdGRvdXQ9UElQRSwgc3RkaW49UElQRSwgc3RkZXJyPVNURE9VVAogICAgKQogICA"
"gdXVpZF9nZW5lcmF0ZV9wcm9jLmNvbW11bmljYXRlKGlucHV0PSJTeXN0ZW0uTWFuYWdlbWVudC5y"
"ZWdlbmVyYXRlVVVJRCIpWzBdCiAgICBjYWxsKCIlcy9zdG9wLXpldXMiICUgWkVVU0hPTUUpCiAgI"
"CB3aXRoIG9wZW4oIi9yb290L2NvbmZpZ19kYXRhIikgYXMgY29uZmlnX2RyaXZlOgogICAgICAgIH"
"VzZXJfZGF0YSA9IGpzb24ubG9hZHMoY29uZmlnX2RyaXZlLnJlYWQoKSkKICAgIGdsb2JhbF9jb25"
"maWcgPSBDb25maWdGaWxlKCdnbG9iYWwuY2ZnJywgIiVzL3p4dG0iICUgWkVVU0hPTUUpCiAgICBz"
"ZXR0aW5nc19jb25maWcgPSBDb25maWdGaWxlKCdzZXR0aW5ncy5jZmcnLCAiJXMvenh0bS9jb25mI"
"iAlIFpFVVNIT01FKQogICAgc2VjdXJpdHlfY29uZmlnID0gQ29uZmlnRmlsZSgnc2VjdXJpdHknLC"
"AiJXMvenh0bS9jb25mIiAlIFpFVVNIT01FKQogICAgcmVwbGF5X2RhdGEgPSBSZXBsYXlEYXRhKHV"
"zZXJfZGF0YVsncmVwbGF5X2RhdGEnXSkKICAgIGZvciBwYXJhbWV0ZXIgaW4gcmVwbGF5X2RhdGEu"
"dmFsdWVzKCk6CiAgICAgICAgaWYgcGFyYW1ldGVyLmtleSA9PSAiYWRtaW4hcGFzc3dvcmQiOgogI"
"CAgICAgICAgICBwYXNzd29yZF9wcm9jID0gUG9wZW4oCiAgICAgICAgICAgICAgICBbJ3otcmVzZX"
"QtcGFzc3dvcmQnXSwgCiAgICAgICAgICAgICAgICBzdGRvdXQ9UElQRSwgc3RkaW49UElQRSwgc3R"
"kZXJyPVNURE9VVAogICAgICAgICAgICApCiAgICAgICAgICAgIHN0ZG91dCA9IHBhc3N3b3JkX3By"
"b2MuY29tbXVuaWNhdGUoaW5wdXQ9IiVzXG4lcyIgJSAoCiAgICAgICAgICAgICAgICBwYXJhbWV0Z"
"XIudmFsdWVfc3RyLCBwYXJhbWV0ZXIudmFsdWVfc3RyCiAgICAgICAgICAgICkpWzBdCiAgICAgIC"
"AgZWxpZiBwYXJhbWV0ZXIua2V5ID09ICJtb25pdG9yX3VzZXIiOgogICAgICAgICAgICBuZXdfdXN"
"lciA9IHsgCiAgICAgICAgICAgICAgICAidXNlcm5hbWUiOiBwYXJhbWV0ZXIudmFsdWVfbGlzdFsw"
"XSwKICAgICAgICAgICAgICAgICJwYXNzd29yZCI6IHBhcmFtZXRlci52YWx1ZV9saXN0WzFdLAogI"
"CAgICAgICAgICAgICAgImdyb3VwIjogIkd1ZXN0IgogICAgICAgICAgICB9CiAgICAgICAgZWxpZi"
"BwYXJhbWV0ZXIua2V5IGluIFsgJ3Jlc3QhZW5hYmxlZCcsICdjb250cm9sYWxsb3cnIF06CiAgICA"
"gICAgICAgIHNldHRpbmdzX2NvbmZpZ1twYXJhbWV0ZXIua2V5XSA9IHBhcmFtZXRlci52YWx1ZV9z"
"dHIKICAgICAgICBlbGlmIHBhcmFtZXRlci5rZXkgaW4gWyAnZGV2ZWxvcGVyX21vZGVfYWNjZXB0Z"
"WQnLCAnbmFtZWlwJyBdOgogICAgICAgICAgICBnbG9iYWxfY29uZmlnW3BhcmFtZXRlci5rZXldID"
"0gcGFyYW1ldGVyLnZhbHVlX3N0cgogICAgICAgIGVsaWYgcGFyYW1ldGVyLnByZWZpeCBpbiBbICd"
"hcHBsaWFuY2UnLCAncmVzdCcsICdjb250cm9sJyBdOgogICAgICAgICAgICBnbG9iYWxfY29uZmln"
"W3BhcmFtZXRlci5rZXldID0gcGFyYW1ldGVyLnZhbHVlX3N0cgogICAgICAgIGVsaWYgcGFyYW1ld"
"GVyLmtleSBpbiBbICdhY2Nlc3MnIF06CiAgICAgICAgICAgIHNlY3VyaXR5X2NvbmZpZ1twYXJhbW"
"V0ZXIua2V5XSA9IHBhcmFtZXRlci52YWx1ZV9zdHIKICAgIGdsb2JhbF9jb25maWcuYXBwbHkoKQo"
"gICAgc2V0dGluZ3NfY29uZmlnLmFwcGx5KCkKICAgIHNlY3VyaXR5X2NvbmZpZy5hcHBseSgpCiAg"
"ICBvcy5yZW1vdmUoIiVzL3p4dG0vZ2xvYmFsLmNmZyIgJSBaRVVTSE9NRSkKICAgIG9zLnJlbmFtZ"
"SgKICAgICAgICAiJXMvenh0bS9jb25mL3p4dG1zLyhub25lKSIgJSBaRVVTSE9NRSwgCiAgICAgIC"
"AgIiVzL3p4dG0vY29uZi96eHRtcy8lcyIgJSAoWkVVU0hPTUUsIHVzZXJfZGF0YVsnaG9zdG5hbWU"
"nXSkKICAgICkKICAgIG9zLnN5bWxpbmsoCiAgICAgICAgIiVzL3p4dG0vY29uZi96eHRtcy8lcyIg"
"JSAoWkVVU0hPTUUsIHVzZXJfZGF0YVsnaG9zdG5hbWUnXSksIAogICAgICAgICIlcy96eHRtL2dsb"
"2JhbC5jZmciICUgWkVVU0hPTUUKICAgICkKICAgIGNhbGwoWyAiJXMvenh0bS9iaW4vc3lzY29uZm"
"lnIiAlIFpFVVNIT01FLCAiLS1hcHBseSIgXSkKICAgIGNhbGwoIiVzL3N0YXJ0LXpldXMiICUgWkV"
"VU0hPTUUpCiAgICBpZiBuZXdfdXNlciBpcyBub3QgTm9uZToKICAgICAgICB1c2VyX3Byb2MgPSBQ"
"b3BlbigKICAgICAgICAgICAgWyIlcy96eHRtL2Jpbi96Y2xpIiAlIFpFVVNIT01FXSwKICAgICAgI"
"CAgICAgc3Rkb3V0PVBJUEUsIHN0ZGluPVBJUEUsIHN0ZGVycj1TVERPVVQKICAgICAgICApCiAgIC"
"AgICAgdXNlcl9wcm9jLmNvbW11bmljYXRlKGlucHV0PSJVc2Vycy5hZGRVc2VyICVzLCAlcywgJXM"
"iICUgKAogICAgICAgICAgICBuZXdfdXNlclsndXNlcm5hbWUnXSwgbmV3X3VzZXJbJ3Bhc3N3b3Jk"
"J10sIG5ld191c2VyWydncm91cCddCiAgICAgICAgKSlbMF0KICAgIGlmIHVzZXJfZGF0YVsnY2x1c"
"3Rlcl9qb2luX2RhdGEnXSBpcyBub3QgTm9uZToKICAgICAgICB3aXRoIG9wZW4oIi90bXAvcmVwbG"
"F5X2RhdGEiLCAidyIpIGFzIHJlcGxheV9maWxlOgogICAgICAgICAgICByZXBsYXlfZmlsZS53cml"
"0ZSh1c2VyX2RhdGFbJ2NsdXN0ZXJfam9pbl9kYXRhJ10pCiAgICAgICAgY2FsbChbICIlcy96eHRt"
"L2NvbmZpZ3VyZSIgJSBaRVVTSE9NRSwgIi0tcmVwbGF5LWZyb209L3RtcC9yZXBsYXlfZGF0YSIgX"
"SkKCgppZiBfX25hbWVfXyA9PSAiX19tYWluX18iOgogICAgbWFpbigpCg=="
"""
    path: /root/configure.py

runcmd:
-   [ "python", "/root/configure.py" ]
    """ % base64.b64encode(json.dumps(user_data)))
