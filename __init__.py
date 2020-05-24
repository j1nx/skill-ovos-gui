# Copyright 2018 Mycroft AI Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


import arrow

from mycroft.messagebus.message import Message
from mycroft.skills.core import MycroftSkill
from mycroft.util import get_ipc_directory
from mycroft.util.log import LOG
from mycroft.util.parse import normalize
from mycroft import intent_file_handler

import os
import subprocess

import pyaudio
from threading import Thread, Lock

from .listener import (get_rms, open_mic_stream, read_file_from, INPUT_FRAMES_PER_BLOCK)

def compare_origin(m1, m2):
	origin1 = m1.data['__from'] if isinstance(m1, Message) else m1
	origin2 = m2.data['__from'] if isinstance(m2, Message) else m2
	return origin1 == origin2

class MycroftGUI(MycroftSkill):
	"""
		The MycroftGUI skill handles much of the gui activities related to
		Mycroft's core functionality. This includes showing "listening",
		"thinking", and "speaking" faces as well as more complicated things
		such as switching to the selected resting face and handling
		system signals.
	"""
	def __init__(self):
		super().__init__('MycroftGUI')

		self.idle_screens = {}
		self.override_idle = None
		self.idle_next = 0  # Next time the idle screen should trigger
		self.idle_lock = Lock()

		self.settings['use_listening_beep'] = True

		self.has_show_page = False  # resets with each handler

		# Volume indicatior
		self.thread = None
		self.pa = pyaudio.PyAudio()
		try:
			self.listener_file = os.path.join(get_ipc_directory(), 'mic_level')
			self.st_results = os.stat(self.listener_file)
		except Exception:
			self.listener_file = None
			self.st_results = None
			self.max_amplitude = 0.001

	def setup_mic_listening(self):
		""" Initializes PyAudio, starts an input stream and launches the
			listening thread.
		"""
		listener_conf = self.config_core['listener']
		self.stream = open_mic_stream(self.pa,
										listener_conf.get('device_index'),
										listener_conf.get('device_name'))
		self.amplitude = 0

	def initialize(self):
		""" Perform initalization.
			Registers messagebus handlers and sets default gui values.
		"""
		enclosure_info = self.config_core.get('enclosure', {})
        
		self.gui['volume'] = 0

		# Prepare GUI Viseme structure
		self.gui['viseme'] = {'start': 0, 'visemes': []}

		# Preselect Time and Date as resting screen
		self.gui['selected'] = self.settings.get('selected', 'Time and Date')
		self.gui.set_on_gui_changed(self.save_resting_screen)

		try:
			self.add_event('mycroft.internet.connected',
							self.handle_internet_connected)

			# Handle the 'waking' visual
			self.add_event('recognizer_loop:record_begin',
							self.handle_listener_started)
			self.add_event('recognizer_loop:record_end',
							self.handle_listener_ended)
			self.add_event('mycroft.speech.recognition.unknown',
							self.handle_failed_stt)

			# Handle the 'busy' visual
			self.bus.on('mycroft.skill.handler.start',
						self.on_handler_started)

			self.bus.on('recognizer_loop:sleep',
						self.on_handler_sleep)
			self.bus.on('mycroft.awoken',
						self.on_handler_awoken)
			self.bus.on('enclosure.mouth.reset',
						self.on_handler_mouth_reset)
			self.bus.on('recognizer_loop:audio_output_end',
						self.on_handler_mouth_reset)
			self.bus.on('enclosure.mouth.viseme_list',
						self.on_handler_speaking)
			self.bus.on('gui.page.show',
						self.on_gui_page_show)
			self.bus.on('gui.page_interaction',
						self.on_gui_page_interaction)

			self.bus.on('mycroft.skills.initialized',
						self.reset_face)
			self.bus.on('mycroft.mycroft-gui.register_idle',
						self.on_register_idle)

			self.add_event('mycroft.mycroft-gui.reset_idle',
							self.restore_idle_screen)

			# Use Legacy for QuickSetting delegate
			self.gui.register_handler('mycroft.device.show.idle',
										self.show_idle_screen)

			# Handle idle selection
			self.gui.register_handler('mycroft.device.set.idle',
										self.set_idle_screen)

			# Collect Idle screens and display if skill is restarted
			self.collect_resting_screens()
		except Exception:
			LOG.exception('In MycroftGUI Skill')

		# Update use of wake-up beep
		self._sync_wake_beep_setting()

		self.settings_change_callback = self.on_websettings_changed

	def start_listening_thread(self):
		# Start listening thread
		if not self.thread:
			self.running = True
			self.thread = Thread(target=self.listen_thread)
			self.thread.daemon = True
			self.thread.start()

	def stop_listening_thread(self):
		if self.thread:
			self.running = False
			self.thread.join()
			self.thread = None

	###################################################################
	# Idle screen mechanism

	def collect_resting_screens(self):
		""" Trigger collection and then show the resting screen. """
		self.bus.emit(Message('mycroft.mycroft-gui.collect_idle'))
		time.sleep(1)
		self.show_idle_screen()

	def on_register_idle(self, message):
		""" Handler for catching incoming idle screens. """
		if 'name' in message.data and 'id' in message.data:
			self.idle_screens[message.data['name']] = message.data['id']
			self.log.info('Registered {}'.format(message.data['name']))
		else:
			self.log.error('Malformed idle screen registration received')

	def reset_face(self, message):
		""" Triggered after skills are initialized.

			Sets switches from resting "face" to a registered resting screen.
		"""
		time.sleep(1)
		self.collect_resting_screens()

	def listen_thread(self):
		""" listen on mic input until self.running is False. """
		self.setup_mic_listening()
		self.log.debug("Starting listening")
		while(self.running):
			self.listen()
		self.stream.close()
		self.log.debug("Listening stopped")

	def get_listener_level(self):
		""" Get level from IPC file created by listener. """
		time.sleep(0.05)
		if not self.listener_file:
			try:
				self.listener_file = os.path.join(get_ipc_directory(), 'mic_level')
			except FileNotFoundError:
				return None

		try:
			st_results = os.stat(self.listener_file)

			if (not st_results.st_ctime == self.st_results.st_ctime or
					not st_results.st_mtime == self.st_results.st_mtime):
				ret = read_file_from(self.listener_file, 0)
				self.st_results = st_results
				if ret is not None:
					if ret > self.max_amplitude:
						self.max_amplitude = ret
					ret = int(ret / self.max_amplitude * 10)
				return ret
			except Exception as e:
			self.log.error(repr(e))
		return None

	def listen(self):
		""" Read microphone level and store rms into self.gui['volume']. """
		amplitude = self.get_listener_level()

		if (self.gui and
			('volume' not in self.gui or self.gui['volume'] != amplitude) and
				amplitude is not None):
			self.gui['volume'] = amplitude

	def restore_idle_screen(self, _=None):
		if (self.override_idle and
				time.monotonic() - self.override_idle[1] > 2):
			self.override_idle = None
			self.show_idle_screen()

	def stop(self, message=None):
		""" Clear override_idle and stop visemes. """
		self.restore_idle_screen()
		self.gui['viseme'] = {'start': 0, 'visemes': []}
		return False

	def shutdown(self):
		# Gotta clean up manually since not using add_event()
		self.bus.remove('mycroft.skill.handler.start',
						self.on_handler_started)
		self.bus.remove('recognizer_loop:sleep',
						self.on_handler_sleep)
		self.bus.remove('mycroft.awoken',
						self.on_handler_awoken)
		self.bus.remove('enclosure.mouth.reset',
						self.on_handler_mouth_reset)
		self.bus.remove('recognizer_loop:audio_output_end',
						self.on_handler_mouth_reset)
		self.bus.remove('enclosure.mouth.viseme_list',
						self.on_handler_speaking)
		self.bus.remove('gui.page.show',
						self.on_gui_page_show)
		self.bus.remove('gui.page_interaction', self.on_gui_page_interaction)
		self.bus.remove('mycroft.mycroft-gui.register_idle', self.on_register_idle)

		self.stop_listening_thread()

	#####################################################################
	# Manage "busy" visual

	def on_handler_started(self, message):
		handler = message.data.get("handler", "")
		# Ignoring handlers from this skill and from the background clock
		if 'MycroftGUI' in handler:
			return
		if 'TimeSkill.update_display' in handler:
			return

	def on_gui_page_interaction(self, message):
		""" Reset idle timer to 30 seconds when page is flipped. """
		self.log.info("Resetting idle counter to 30 seconds")
		self.start_idle_event(30)

	def on_gui_page_show(self, message):
		if 'mycroft-gui' not in message.data.get('__from', ''):
			# Some skill other than the handler is showing a page
			self.has_show_page = True

			# If a skill overrides the idle do not switch page
			override_idle = message.data.get('__idle')
			if override_idle is True:
				# Disable idle screen
				self.log.info('Cancelling Idle screen')
				self.cancel_idle_event()
				self.override_idle = (message, time.monotonic())

			elif isinstance(override_idle, int) and override_idle is not False:
				# Set the indicated idle timeout
				self.log.info('Overriding idle timer to'
								' {} seconds'.format(override_idle))
				self.start_idle_event(override_idle)
			elif (message.data['page'] and
					not message.data['page'][0].endswith('idle.qml')):
				# Check if the show_page deactivates a previous idle override
				# This is only possible if the page is from the same skill
				if (override_idle is False and
						compare_origin(message, self.override_idle[0])):
					# Remove the idle override page if override is set to false
					self.override_idle = None
				# Set default idle screen timer
				self.start_idle_event(30)

	def on_handler_mouth_reset(self, message):
		""" Restore viseme to a smile. """
		pass

	def on_handler_sleep(self, message):
		""" Show resting face when going to sleep. """
		self.gui['state'] = 'resting'
		self.gui.show_page('all.qml')

	def on_handler_awoken(self, message):
		""" Show awake face when sleep ends. """
		self.gui['state'] = 'awake'
		self.gui.show_page('all.qml')

	def on_handler_complete(self, message):
		""" When a skill finishes executing clear the showing page state. """
		handler = message.data.get('handler', '')
		# Ignoring handlers from this skill and from the background clock
		if 'MycroftGUI' in handler:
			return
		if 'TimeSkill.update_display' in handler:
			return

		self.has_show_page = False

		try:
			if self.hourglass_info[handler] == -1:
				self.enclosure.reset()
			del self.hourglass_info[handler]
		except Exception:
			# There is a slim chance the self.hourglass_info might not
			# be populated if this skill reloads at just the right time
			# so that it misses the mycroft.skill.handler.start but
			# catches the mycroft.skill.handler.complete
			pass

    #####################################################################
    # Manage "speaking" visual

	def on_handler_speaking(self, message):
		""" Show the speaking page if no skill has registered a page
			to be shown in it's place.
		"""
		self.gui["viseme"] = message.data
		if not self.has_show_page:
			self.gui['state'] = 'speaking'
			self.gui.show_page("all.qml")
			# Show idle screen after the visemes are done (+ 2 sec).
			time = message.data['visemes'][-1][1] + 5
			self.start_idle_event(time)

    #####################################################################
    # Manage "idle" visual state
	def cancel_idle_event(self):
		self.idle_next = 0
		self.cancel_scheduled_event('IdleCheck')

	def start_idle_event(self, offset=60, weak=False):
		""" Start an event for showing the idle screen.

		Arguments:
			offset: How long until the idle screen should be shown
			weak: set to true if the time should be able to be overridden
		"""
		with self.idle_lock:
		if time.monotonic() + offset < self.idle_next:
			self.log.info('No update, before next time')
			return

			self.log.info('Starting idle event')
			try:
				if not weak:
					self.idle_next = time.monotonic() + offset
				# Clear any existing checker
				self.cancel_scheduled_event('IdleCheck')
				time.sleep(0.5)
				self.schedule_event(self.show_idle_screen, int(offset),
									name='IdleCheck')
				self.log.info('Showing idle screen in '
								'{} seconds'.format(offset))
			except Exception as e:
				self.log.exception(repr(e))

	def show_idle_screen(self):
		""" Show the idle screen or return to the skill that's overriding idle.
		"""
		self.log.debug('Showing idle screen')
		screen = None
		if self.override_idle:
			self.log.debug('Returning to override idle screen')
			# Restore the page overriding idle instead of the normal idle
			self.bus.emit(self.override_idle[0])
		elif len(self.idle_screens) > 0 and 'selected' in self.gui:
			# TODO remove hard coded value
			self.log.debug('Showing Idle screen for '
							'{}'.format(self.gui['selected']))
			screen = self.idle_screens.get(self.gui['selected'])
			if screen:
			self.bus.emit(Message('{}.idle'.format(screen)))

	def handle_listener_started(self, message):
		""" Shows listener page after wakeword is triggered.

			Starts countdown to show the idle page.
		"""
		# Start idle timer
		self.cancel_idle_event()
		self.start_idle_event(weak=True)

		# Lower the max by half at the start of listener to make sure
		# loud noices doesn't make the level stick to much
		if self.max_amplitude > 0.001:
			self.max_amplitude /= 2

		self.start_listening_thread()
		# Show listening page
		self.gui['state'] = 'listening'
		self.gui.show_page('all.qml')

	def handle_listener_ended(self, message):
		""" When listening has ended show the thinking animation. """
		self.has_show_page = False
		self.gui['state'] = 'thinking'
		self.gui.show_page('all.qml')
		self.stop_listening_thread()

	def handle_failed_stt(self, message):
		""" No discernable words were transcribed. Show idle screen again. """
		self.show_idle_screen()

    #####################################################################
    # Manage network connction feedback

	def handle_internet_connected(self, message):
		""" System came online later after booting. """
		self.enclosure.mouth_reset()

    #####################################################################
    # Web settings

	def on_websettings_changed(self):
		""" Update use of wake-up beep. """
		self._sync_wake_beep_setting()

	def _sync_wake_beep_setting(self):
		""" Update "use beep" global config from skill settings. """
		from mycroft.configuration.config import (
			LocalConf, USER_CONFIG, Configuration
		)
		config = Configuration.get()
		use_beep = self.settings.get('use_listening_beep') is True
		if not config['confirm_listening'] == use_beep:
			# Update local (user) configuration setting
			new_config = {
				'confirm_listening': use_beep
			}
			user_config = LocalConf(USER_CONFIG)
			user_config.merge(new_config)
			user_config.store()
			self.bus.emit(Message('configuration.updated'))


def create_skill():
	return MycroftGUI()
