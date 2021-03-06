#!/usr/bin/env python

import os
import errno
import subprocess
import sys
import yaml
import shutil
import urllib
import base64
import zipfile

def build(config):
	with cd('release', clobber=True):
		release = create_bosh_release(config)
	print
	with cd('product', clobber=True):
		create_tile(config, release)

def create_tile(config, release):
	release['file'] = os.path.basename(release['tarball'])
	with cd('releases'):
		print 'tile generate release'
		shutil.copy(release['tarball'], release['file'])
	with cd('metadata'):
		print 'tile generate metadata'
		metadata = tile_metadata(config, release)
		with open(release['name'] + '.yml', 'wb') as f:
			write_yaml(f, metadata)
	with cd('content_migrations'):
		print 'tile generate content-migrations'
		migrations = tile_migrations(config, release)
		with open(release['name'] + '.yml', 'wb') as f:
			write_yaml(f, migrations)
	pivotal_file = release['name'] + '-' + release['version'] + '.pivotal'
	with zipfile.ZipFile(pivotal_file, 'w') as f:
		f.write(os.path.join('releases', release['file']))
		f.write(os.path.join('metadata', release['name'] + '.yml'))
		f.write(os.path.join('content_migrations', release['name'] + '.yml'))
	print
	print 'created tile', pivotal_file

default_vm_definitions = [
	{ 'name': 'cpu', 'type': 'integer', 'configurable': False, 'default': 1 },
	{ 'name': 'ram', 'type': 'integer', 'configurable': False, 'default': 1024 },
	{ 'name': 'ephemeral_disk', 'type': 'integer', 'configurable': False, 'default': 2048 },
	{ 'name': 'persistent_disk', 'type': 'integer', 'configurable': False, 'default': 0 },
]
default_instance_definitions = [
	{ 'name': 'instances', 'type': 'integer', 'configurable': False, 'default': 1 },
]

def tile_metadata(config, release):
	metadata = {
		'name': release['name'],
		'product_version': release['version'],
		'metadata_version': '1.5',
		'label': config['label'],
		'description': config['description'],
		'icon_image': config['icon_image'],
		'rank': 1,
		'stemcell_criteria': {
			'os': 'ubuntu-trusty',
			'requires_cpi': False,
			'version': '3062',
		},
		'releases': [{
			'name': release['name'],
			'file': release['file'],
			'version': release['version'],
		}],
		'job_types': [{
			'name': 'compilation',
			'resource_label': 'compilation',
			'static_ip': 0,
			'dynamic_ip': 1,
			'max_in_flight': 1,
			'resource_definitions': default_vm_definitions,
			'instance_definitions': default_instance_definitions,
		}]
	}
	metadata['stemcell_criteria'].update(config.get('stemcell_criteria', {}))
	for bp in config.get('buildpacks', []):
		metadata['form_types'] = metadata.get('form_types', [{
			'name': 'buildpack_properties',
			'label': 'Buildpack',
			'description': 'Buildpack Properties',
			'property_inputs': []
		}])
		metadata['form_types'][0]['property_inputs'] += [{
			'reference': '.properties.' + bp['name'] + '_rank',
			'label': 'Buildpack rank for ' + bp['name'],
			'description': 'Ranking of this buildpack relative to others'
		}]
		metadata['property_blueprints'] = metadata.get('property_blueprints', [])
		metadata['property_blueprints'] += [{
			'name': bp['name'] + '_rank',
			'type': 'integer',
			'configurable': True,
			'default': bp['rank'],
		}]
		metadata['job_types'] += [ create_errand(metadata, {
			'name': 'install_' + bp['name'],
			'resource_label': 'Install ' + bp['name'],
			'templates': [{ 'name': 'install_' + bp['name'], 'release': release['name'] }],
		}, [
			bp['name'] + '_rank: (( .properties.' + bp['name'] + '_rank.value ))'
		])]
		metadata['job_types'] += [ create_errand(metadata, {
			'name': 'remove_' + bp['name'],
			'resource_label': 'Remove ' + bp['name'],
			'templates': [{ 'name': 'remove_' + bp['name'], 'release': release['name'] }],
		})]
		metadata['post_deploy_errands'] = metadata.get('post_deploy_errands', [])
		metadata['post_deploy_errands'] += [{ 'name': 'install_' + bp['name'] }]
		metadata['pre_delete_errands'] = metadata.get('pre_delete_errands', [])
		metadata['pre_delete_errands'] += [{ 'name': 'remove_' + bp['name'] }]
	return metadata

def tile_migrations(config, release):
	migrations = {
		'product': release['name'],
		'installation_schema_version': '1.5',
		'to_version': release['version'],
		'migrations': [
			{
				'from_version': prior,
				'rules': [{
					'type': 'update',
					'selector': 'product_version',
					'to': release['version']
				}]
			}
			for prior in config.get('history', []) if prior != release['version']
		]
	}
	return migrations

def create_errand(metadata, properties, manifest_lines=[] ):
	print 'tile generate errand', properties['name']
	errand = {
		'errand': True,
		'resource_definitions': default_vm_definitions,
		'instance_definitions': default_instance_definitions,
		'static_ip': 0,
		'dynamic_ip': 1,
		'max_in_flight': 1,
		'property_blueprints': [{
			'name': 'vm_credentials',
			'type': 'salted_credentials',
			'default': { 'identity': 'vcap' }
		}],
		'manifest': '      ' + '\n      '.join(
			manifest_lines + [
				'ssl:',
				'  skip_cert_verify: (( ..cf.ha_proxy.skip_cert_verify.value ))',
				'cf:',
				'  domain: (( ..cf.cloud_controller.system_domain.value ))',
				'  admin_user: (( ..cf.uaa.system_services_credentials.identity ))',
				'  admin_password: (( ..cf.uaa.system_services_credentials.password ))'
			]
		)
	}
	errand.update(properties)
	return errand

def create_bosh_release(config):
	bosh('init', 'release')
	add_bosh_config(config)
	add_cf_cli()
	add_buildpacks(config)
	add_service_brokers(config)
	output = bosh('create', 'release', '--final', '--with-tarball', '--version', config['version'])
	return bosh_extract(output, [
		{ 'label': 'name', 'pattern': 'Release name' },
		{ 'label': 'version', 'pattern': 'Release version' },
		{ 'label': 'manifest', 'pattern': 'Release manifest' },
		{ 'label': 'tarball', 'pattern': 'Release tarball' },
	])

def bosh_extract(output, properties):
	result = {}
	for l in output.split('\n'):
		for p in properties:
			if l.startswith(p['pattern']):
				result[p['label']] = l.split(':', 1)[-1].strip()
	return result

def add_bosh_config(config):
	spec = {
		'blobstore': {
			'provider': 'local',
			'options': {
				'blobstore_path': '/tmp/unused-blobs'
			}
		},
		'final_name': config['name']
	}
	with cd('config'):
		with open('final.yml', 'wb') as f:
			write_yaml(f, spec)

def add_buildpacks(config):
	buildpacks = config.get('buildpacks', [])
	for bp in buildpacks:
		validate_buildpack(bp)
		add_src_package(bp['name'], bp['binary'])
		add_bosh_job('install_' + bp['name'], [
				'cf api https://api.' +
					bosh_property('cf.domain') +
					bosh_property_if('ssl.skip_cert_verify', ' --skip-ssl-validation'),
				'cf auth ' +
					bosh_property('cf.admin_user') + ' ' +
					bosh_property('cf.admin_password'),
				'cf delete-buildpack ' + 
					bp['name'] + ' -f',
				'cf create-buildpack ' +
					bp['name'] + ' /var/vcap/packages/' + bp['name'] + '/' + os.path.basename(bp['binary']) + ' ' +
					bosh_property(bp['name'] + '_rank') +
					' --enable',
			],
			properties = {
				bp['name'] + '_rank': {
					'description': 'The relative ranking of ' + bp['name'],
					'default': bp['rank'],
				}
			},
			dependencies = [ bp['name'] ])
		add_bosh_job('remove_' + bp['name'], [
				'cf api https://api.' +
					bosh_property('cf.domain') +
					bosh_property_if('ssl.skip_cert_verify', ' --skip-ssl-validation'),
				'cf auth ' +
					bosh_property('cf.admin_user') + ' ' +
					bosh_property('cf.admin_password'),
				'cf delete-buildpack ' + 
					bp['name'] + ' -f',
			],
			dependencies = [ bp['name'] ])

def validate_buildpack(bp):
	bp['name']   = bp.get('name', None)
	bp['binary'] = bp.get('binary', None)
	bp['rank']   = bp.get('rank', '0')
	if bp['name'] is None or bp['binary'] is None:
		print >> sys.stderr, 'Each buildpack entry must specify a name, a binary, and optionally a default rank'
		sys.exit(1)
	if len(bp['name']) > 20 or not bp['name'].endswith('_buildpack'):
		print >> sys.stderr, bp['name'], '- Buildpack names are by convention short and end with _buildpack'
		sys.exit(1)

def bosh_property(key):
	return '<%= properties.' + key + ' %>'

def bosh_property_if(key, body):
	return '<% if properties.' + key + ' %>' + body + '<% end %>'

def add_bosh_job(name, commands, properties={}, dependencies=[]):
	commands = [
			'#!/bin/bash',
			'set -e -x',
			'export PATH="/var/vcap/packages/cf_cli/bin:$PATH"',
			'export CF_HOME=`pwd`/home/cf',
			'mkdir -p $CF_HOME',
		] + commands
	bosh('generate', 'job', name)
	shname = name + '.sh.erb'
	spec = {
		'name': name,
		'templates': { shname: 'bin/run' },
		'packages': [ 'cf_cli' ] + dependencies,
		'properties' : {
			'ssl.skip_cert_verify': { 'description': 'Whether to verify SSL certs when making web requests' },
			'cf.domain': { 'Cloud Foundry system domain' },
			'cf.admin_user': { 'Username of the CF admin user' },
			'cf.admin_password': { 'Password for the CF admin user' },
		}
	}
	spec['properties'].update(properties)
	jobdir = os.path.join('jobs', name)
	with cd(jobdir):
		with open('monit', 'wb') as f:
			pass
		with open('spec', 'wb') as f:
			write_yaml(f, spec)
		with cd('templates'):
			with open(shname, 'wb') as f:
				for c in commands:
					f.write(c + '\n')

def add_src_package(name, binary, url=None, commands=None):
	bosh('generate', 'package', name)
	srcdir = os.path.join('src', name)
	pkgdir = os.path.join('packages', name)
	mkdir_p(srcdir)
	if url is None:
		shutil.copy(os.path.join('..', binary), srcdir)
	else:
		urllib.urlretrieve(url, os.path.join(srcdir, binary))
	spec = {
		'name': name,
		'dependencies': [],
		'files': [ name + '/*' ],
	}
	with cd(pkgdir):
		with open('packaging', 'wb') as f:
			if commands is None:
				f.write('cp ' + name + '/* ${BOSH_INSTALL_TARGET}')
			else:
				for c in commands:
					f.write(c + '\n')
		with open('pre_packaging', 'wb') as f:
			pass
		with open('spec', 'wb') as f:
			write_yaml(f, spec)

def add_service_brokers(config):
	brokers = config.get('service-brokers', None)
	if brokers is None:
		return
	print >> sys.stderr, 'Service broker support is not yet implemented'
	sys.exit(1)

def add_cf_cli():
	add_src_package(
		'cf_cli',
		'cf-linux-amd64.tgz',
		url='https://cli.run.pivotal.io/stable?release=linux64-binary&source=github-rel',
		commands=[
			'set -e -x',
			'mkdir -p ${BOSH_INSTALL_TARGET}/bin',
			'cd cf_cli',
			'tar zxvf cf-linux-amd64.tgz',
			'cp cf ${BOSH_INSTALL_TARGET}/bin/'
		]
	)

def validate_config(config, key, message):
	value = config.get(key, None)
	print key + ':', message if value is None else value
	return value is not None

def read_config():
	try:
		with open(CONFIG_FILE) as config_file:
			return read_yaml(config_file)
	except IOError as e:
		print >> sys.stderr, 'Not a tile repository. Use "tile init" in the root of your repository to create one.'
		sys.exit(1)

def write_config(config):
	with open(CONFIG_FILE, 'wb') as config_file:
		write_yaml(config_file, config)

def read_yaml(file):
	return yaml.safe_load(file)

def write_yaml(file, data):
	file.write('---\n')
	file.write(yaml.dump(data, default_flow_style=False))

def check_status(config):
	status = True
	status &= validate_config(config, 'name', '<use "tile set-name" to specify the tile name>')
	status &= validate_config(config, 'icon', '<use "tile set-icon" to specify the tile icon>')
	status &= validate_config(config, 'label', '<use "tile set-label" to specify the tile label>')
	status &= validate_config(config, 'description', '<use "tile set-description" to specify the tile description>')
	if not status:
		sys.exit(1)

def is_semver(version):
	semver = version.split('.')
	if len(semver) != 3:
		return False
	try:
		int(semver[0])
		int(semver[1])
		int(semver[2])
		return True
	except:
		return False

def update_version(config, version):
	prior_version = config.get('version', None)
	if prior_version is not None:
		config['history'] = config.get('history', [])
		config['history'] += [ prior_version ]
	if not is_semver(version):
		semver = config.get('version', '0.0.0')
		if not is_semver(semver):
			print >>sys.stderr, 'Version must be in semver format (x.y.z), instead found', semver
		semver = semver.split('.')
		if version == 'patch':
			semver[2] = str(int(semver[2]) + 1)
		elif version == 'minor':
			semver[1] = str(int(semver[1]) + 1)
			semver[2] = '0'
		elif version == 'major':
			semver[0] = str(int(semver[0]) + 1)
			semver[1] = '0'
			semver[2] = '0'
		else:
			print >>sys.stderr, 'Argument must specify "patch", "minor", "major", or a valid semver version (x.y.z)'
			sys.exit(1)
		version = '.'.join(semver)
	config['version'] = version
	print 'version:', version

def bosh(*argv):
	argv = list(argv)
	print 'bosh', ' '.join(argv)
	command = [ 'bosh', '--no-color', '--non-interactive' ] + argv
	try:
		return subprocess.check_output(command, stderr=subprocess.STDOUT)
	except subprocess.CalledProcessError as e:
		if argv[0] == 'init' and argv[1] == 'release' and 'Release already initialized' in e.output:
			return e.output
		if argv[0] == 'generate' and 'already exists' in e.output:
			return e.output
		print e.output
		sys.exit(e.returncode)

class cd:
    """Context manager for changing the current working directory"""
    def __init__(self, newPath, clobber=False):
    	self.clobber = clobber
        self.newPath = os.path.expanduser(newPath)

    def __enter__(self):
        self.savedPath = os.getcwd()
        if os.path.isdir(self.newPath):
			shutil.rmtree(self.newPath)
        mkdir_p(self.newPath)
        os.chdir(self.newPath)

    def __exit__(self, etype, value, traceback):
        os.chdir(self.savedPath)

def mkdir_p(dir):
   try:
      os.makedirs(dir)
   except os.error, e:
      if e.errno != errno.EEXIST:
         raise

""" Commands """

def init_cmd(argv):
	if len(argv) > 1:
		name = argv[1]
		os.mkdir(name)
		os.chdir(name)
	if os.path.isfile(CONFIG_FILE):
		print >>sys.stderr, 'Already initialized.'
		sys.exit(0)
	name = os.path.basename(os.getcwd())
	config = {
		'name': name
	}
	write_config(config)

def status_cmd(argv):
	config = read_config()
	check_status(config)
	version = config.get('version', None)
	if version is not None:
		print 'last build:', version
	stemcell = config.get('stemmcell_criteria', None)
	if stemcell is not None:
		print 'stemcell:', stemcell.get('os', ''), stemcell.get('version', '<unspecified>')
	buildpacks = config.get('buildpacks', None)
	if buildpacks is not None:
		print 'buildpacks:'
		for bp in buildpacks:
			print '   ', bp['name']

def build_cmd(argv):
	config = read_config()
	check_status(config)
	update_version(config, argv[1] if len(argv) > 1 else 'patch')
	print
	build(config)
	write_config(config)

def clean_cmd(argv):
	if not os.path.isfile(CONFIG_FILE):
		print >>sys.stderr, 'Not a tile repository'
		sys.exit(1)
	shutil.rmtree('release')

def clobber_cmd(argv):
	if not os.path.isfile(CONFIG_FILE):
		print >>sys.stderr, 'Not a tile repository'
		sys.exit(1)
	shutil.rmtree('release')
	shutil.rmtree('product')

def set_name_cmd(argv):
	cli.exit_with_usage(argv) if len(argv) < 2 or len(argv) > 2 else None
	config = read_config()
	config['name'] = argv[1]
	write_config(config)

def set_icon_cmd(argv):
	cli.exit_with_usage(argv) if len(argv) < 2 or len(argv) > 2 else None
	with open(argv[1], 'rb') as f:
		icon_image = base64.b64encode(f.read())
	config = read_config()
	config['icon'] = os.path.basename(argv[1])
	config['icon_image'] = icon_image
	write_config(config)

def set_label_cmd(argv):
	cli.exit_with_usage(argv) if len(argv) < 2 else None
	config = read_config()
	config['label'] = ' '.join(argv[1:])
	write_config(config)

def set_description_cmd(argv):
	cli.exit_with_usage(argv) if len(argv) < 2 else None
	config = read_config()
	config['description'] = ' '.join(argv[1:])
	write_config(config)

def update_stemcell_cmd(argv):
	cli.exit_with_usage(argv) if len(argv) < 2 or len(argv) > 3 else None
	config = read_config()
	config['stemcell_criteria'] = config.get('stemcell_criteria', {})
	if len(argv) < 3:
		config['stemcell_criteria']['version'] = argv[2]
	else:
		config['stemcell_criteria']['os'] = argv[2]
		config['stemcell_criteria']['version'] = argv[3]
	config['description'] = ' '.join(argv[1:])
	write_config(config)

def update_buildpack(argv, update_existing=True):
	cli.exit_with_usage(argv) if len(argv) < 3 or len(argv) > 4 else None
	name = argv[1]
	binary = argv[2]
	rank = int(argv[3]) if len(argv) > 3 else 0
	config = read_config()
	buildpacks = config.get('buildpacks', [])
	others = [bp for bp in buildpacks if bp['name'] != name]
	if not update_existing and len(others) < len(buildpacks):
		print >>sys.stderr, 'Buildpack', name, 'already exists. Use "tile update-buildpack" instead.'
		sys.exit(1)
	if update_existing and len(others) == len(buildpacks):
		print >>sys.stderr, 'Buildpack', name, 'does not exist. Use "tile add-buildpack" instead.'
		sys.exit(1)
	config['buildpacks'] = others + [{
		'name': name,
		'binary': binary,
		'rank': rank
	}]
	write_config(config)

def add_buildpack_cmd(argv):
	update_buildpack(argv, False)

def update_buildpack_cmd(argv):
	update_buildpack(argv, True)

def delete_buildpack_cmd(argv):
	cli.exit_with_usage(argv) if len(argv) < 2 or len(argv) > 2 else None
	name = argv[1]
	config = read_config()
	buildpacks = config.get('buildpacks', [])
	others = [bp for bp in buildpacks if bp['name'] != name]
	if len(others) == len(buildpacks):
		print >>sys.stderr, 'Buildpack', name, 'does not exist'
		sys.exit(1)
	config['buildpacks'] = others
	write_config(config)
