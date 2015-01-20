from __future__ import absolute_import
from . import Instance
import virtualbox as vboxapi
import logging
log = logging.getLogger(__name__)


class VirtualBoxInstance(Instance):

	cpus = 1
	memory = 256

	def __init__(self, name, image):
		super(VirtualBoxInstance, self).__init__(name, image)
		self.vbox = vboxapi.VirtualBox()
		manager = vboxapi.Manager()
		self.session = manager.get_session()

	def create(self):
		log.debug('Creating vbox machine `{name}\''.format(name=self.name))
		# create machine
		os_type = {'x86': 'Debian',
		           'amd64': 'Debian_64'}.get(self.image.manifest.system['architecture'])
		self.machine = self.vbox.create_machine(settings_file='', name=self.name,
		                                        groups=[], os_type_id=os_type, flags='')
		self.machine.cpu_count = self.cpus
		self.machine.memory_size = self.memory
		self.machine.save_settings()  # save settings, so that we can register it
		self.vbox.register_machine(self.machine)

		# attach image
		log.debug('Attaching SATA storage controller to vbox machine `{name}\''.format(name=self.name))
		with self.Lock(self.machine, self.session) as machine:
			strg_ctrl = machine.add_storage_controller('SATA Controller',
			                                           vboxapi.library.StorageBus.sata)
			strg_ctrl.port_count = 1
			machine.attach_device(name='SATA Controller', controller_port=0, device=0,
			                      type_p=vboxapi.library.DeviceType.hard_disk,
			                      medium=self.image.medium)
			machine.save_settings()

		# redirect serial port
		log.debug('Enabling serial port on vbox machine `{name}\''.format(name=self.name))
		with self.Lock(self.machine, self.session) as machine:
			serial_port = machine.get_serial_port(0)
			serial_port.enabled = True
			import tempfile
			handle, self.serial_port_path = tempfile.mkstemp()
			import os
			os.close(handle)
			serial_port.path = self.serial_port_path
			serial_port.host_mode = vboxapi.library.PortMode.host_pipe
			serial_port.server = True  # Create the socket on startup
			machine.save_settings()

	def boot(self):
		log.debug('Booting vbox machine `{name}\''.format(name=self.name))
		self.machine.launch_vm_process(self.session, 'headless').wait_for_completion(-1)
		from ..tools import read_from_socket
		# Gotta figure out a more reliable way to check when the system is done booting.
		# Maybe bootstrapped unit test images should have a startup script that issues
		# a callback to the host.
		from bootstrapvz.common.tools import get_codename
		if get_codename(self.image.manifest.system['release']) in ['squeeze', 'wheezy']:
			termination_string = 'INIT: Entering runlevel: 2'
		else:
			termination_string = 'Debian GNU/Linux'
		self.console_output = read_from_socket(self.serial_port_path, termination_string, 120)

	def shutdown(self):
		log.debug('Shutting down vbox machine `{name}\''.format(name=self.name))
		self.session.console.power_down().wait_for_completion(-1)
		self.Lock(self.machine, self.session).unlock()

	def destroy(self):
		log.debug('Destroying vbox machine `{name}\''.format(name=self.name))
		if hasattr(self, 'machine'):
			try:
				log.debug('Detaching SATA storage controller from vbox machine `{name}\''.format(name=self.name))
				with self.Lock(self.machine, self.session) as machine:
					machine.detach_device(name='SATA Controller', controller_port=0, device=0)
					machine.save_settings()
			except vboxapi.library.VBoxErrorObjectNotFound:
				pass
			log.debug('Unregistering and removing vbox machine `{name}\''.format(name=self.name))
			self.machine.unregister(vboxapi.library.CleanupMode.unregister_only)
			self.machine.remove(delete=True)
		else:
			log.debug('vbox machine `{name}\' was not created, skipping destruction'.format(name=self.name))

	def up(self):
		try:
			self.create()
			try:
				self.boot()
			except (Exception, KeyboardInterrupt):
				self.shutdown()
				raise
		except (Exception, KeyboardInterrupt):
			self.destroy()
			raise

	def down(self):
		self.shutdown()
		self.destroy()

	def __enter__(self):
		self.up()
		return self

	def __exit__(self, type, value, traceback):
		self.down()

	class Lock(object):
		def __init__(self, machine, session):
			self.machine = machine
			self.session = session

		def __enter__(self):
			return self.lock()

		def __exit__(self, type, value, traceback):
			return self.unlock()

		def lock(self):
			self.machine.lock_machine(self.session, vboxapi.library.LockType.write)
			return self.session.machine

		def unlock(self):
			from ..tools import waituntil
			if self.machine.session_state == vboxapi.library.SessionState.unlocked:
				return
			if self.machine.session_state == vboxapi.library.SessionState.unlocking:
				waituntil(lambda: self.machine.session_state == vboxapi.library.SessionState.unlocked)
				return
			if self.machine.session_state == vboxapi.library.SessionState.spawning:
				waituntil(lambda: self.machine.session_state == vboxapi.library.SessionState.locked)
			self.session.unlock_machine()
			waituntil(lambda: self.machine.session_state == vboxapi.library.SessionState.unlocked)
