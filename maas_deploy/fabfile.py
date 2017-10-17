##
# Fabric module to deploy MaaS. Run as root user.
#

import os
import sys
import time
import logging

from fabric.api import *
from fabric.operations import reboot
from fabric.colors import cyan, green, red
from fabric.context_managers import shell_env
from fabric.contrib.files import append, sed, comment
from fabric.decorators import hosts, parallel, serial

logging.basicConfig(level=logging.ERROR)
para_log = logging.getLogger('paramiko.transport')
para_log.setLevel(logging.ERROR)

env.roledefs = { 'controller' : ['hostname@ipaddress'] }

@roles('controller')
def install_maas():
	"""Installs MaaS on a remote machine."""
	sudo('add-apt-repository ppa:maas-maintainers/stable')
	sudo('apt-get update')
	sudo('apt-get install -y maas maas-dhcp maas-dns')
	path_to_configs = '/home/user/maas'
	answer = 'unknown'
	while answer != 'y' or answer != 'n':
		eth_name = raw_input("Please specify ethernet device name for wakeonlan: ")
		print(cyan('Ethernet device for wakeonlan is set to: ' + eth_name))
		answer = raw_input("Correct? [y/n]:")
		if answer == 'y':
			put(path_to_configs + '/ether_wake.template', '/tmp/ether_wake.template')
			config_file = '/tmp/ether_wake.template'
			searchExp = '/usr/sbin/etherwake \$mac_address'
			replaceExp = 'sudo /usr/sbin/etherwake -i ' + eth_name + ' \$mac_address'
			sed(config_file, searchExp, replaceExp)
			sudo('mv /tmp/ether_wake.template /etc/maas/templates/power/ether_wake.template')
			run('rm -rf ' + config_file + '.bak')
			put(path_to_configs + '/99-maas-sudoers', '/tmp/99-maas-sudoers')
			config_file = '/tmp/99-maas-sudoers'
			text = 'maas ALL= NOPASSWD: /usr/sbin/etherwake'
			append(config_file, text, use_sudo=True, partial=True, escape=True, shell=False)
			sudo('mv /tmp/99-maas-sudoers /etc/sudoers.d/99-maas-sudoers')
			run('rm -rf ' + config_file + '.bak')
			print(green('Wakeonlan configured. Maas is installed properly.'))
			return
		else:
			print(green('Maas is installed properly.'))
			print(red('Alert: Didn\'t setup wakeonlan. Hit possibility of not being able to do wakeonlan properly.\nPlease do manual configuration!'))
			return
