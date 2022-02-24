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

import time
import os
import subprocess
import secrets
import string
import socket

from json_database import JsonStorage
from os.path import join, dirname, abspath

from mycroft.configuration.config import LocalConf, USER_CONFIG, Configuration
from mycroft.messagebus.message import Message
from mycroft.util.log import LOG
from mycroft import MycroftSkill, intent_handler
from ovos_utils.system import system_reboot, system_shutdown, ssh_enable, ssh_disable
from mycroft.api import DeviceApi, is_paired, check_remote_pairing

class OVOSGuiControlSkill(MycroftSkill):
    """
    The OVOSGuiControl skill handles additional gui activities related to Mycroft's
    core functionality.
    """

    def __init__(self):
        super().__init__("OVOSGuiControl")

        self.settings["auto_brightness"] = False
        self.settings["use_listening_beep"] = True

        # Dashboard Specific
        self.dash_running = None
        alphabet = string.ascii_letters + string.digits
        self.dash_secret = ''.join(secrets.choice(alphabet) for i in range(5))

    def initialize(self):
        """Perform initalization.

        Registers messagebus handlers and sets default gui values.
        """

        self.gui["volume"] = 0

        # Prepare GUI Viseme structure
        self.gui["viseme"] = {"start": 0, "visemes": []}
        
        store_conf = join(self.file_system.path, 'skill_conf.json')
        if not self.file_system.exists("skill_conf.json"):
            self.skill_conf = JsonStorage(store_conf)
            self.skill_conf["selected_backend"] = "unknown"
            self.skill_conf.store()
        else:
            self.skill_conf = JsonStorage(store_conf)

        try:
            self.bus.on("ovos.pairing.process.completed", self.start_homescreen_process)
            self.bus.on("ovos.pairing.set.backend", self.set_backend_type)

            # Handle device settings events
            self.add_event("mycroft.device.settings", self.handle_device_settings)
            
            # Handle GUI release events
            self.add_event("mycroft.gui.screen.close", self.handle_remove_namespace)

            # Use Legacy for QuickSetting delegate
            self.gui.register_handler("mycroft.device.settings", 
                                      self.handle_device_settings)
            self.gui.register_handler("mycroft.device.settings.homescreen",
                                      self.handle_device_homescreen_settings)
            
            self.gui.register_handler('mycroft.device.settings.ssh',
                                      self.handle_device_ssh_settings)
            
            self.gui.register_handler("mycroft.device.settings.restart", 
                                      self.handle_device_restart_action)
            self.gui.register_handler("mycroft.device.settings.poweroff", 
                                      self.handle_device_poweroff_action)
            self.gui.register_handler("mycroft.device.settings.developer", self.handle_device_developer_settings)
            self.gui.register_handler("mycroft.device.enable.dash", self.handle_device_developer_enable_dash)
            self.gui.register_handler("mycroft.device.disable.dash", self.handle_device_developer_disable_dash)

            # System events
            self.add_event("system.reboot",
                           self.handle_system_reboot)
            self.add_event("system.shutdown",
                           self.handle_system_shutdown)
            self.add_event("system.display.homescreen",
                           self.handle_remove_namespace)

            self.device_paired = is_paired()
            self.device_backend = self.skill_conf["selected_backend"]

            if not self.device_backend == "local":
                if self.device_paired:
                    LOG.info("Device Backend Local & Paired")
            else:
                LOG.info("Device Backend Selene")
                self.bus.emit(Message("ovos.shell.status.ok"))

        except Exception:
            LOG.exception("In OVOSGuiControl Skill")

    ###################################################################
    # System events
    def handle_system_reboot(self, _):
        self.speak_dialog("rebooting", wait=True)
        system_reboot()

    def handle_system_shutdown(self, _):
        system_shutdown()
        
    def handle_remove_namespace(self, message):
        self.log.info("Got Clear Namespace Event In Skill")
        get_skill_namespace = message.data.get("skill_id", "")
        if get_skill_namespace:
            self.bus.emit(Message("gui.clear.namespace",
                                  {"__from": get_skill_namespace}))

    ###################################################################
    # Idle screen mechanism
    
    def set_backend_type(self, message):
        backend = message.data.get("backend", "unknown")
        if not backend == "unknown":
            self.skill_conf["selected_backend"] = backend
            self.skill_conf.store()
            self.device_backend = self.skill_conf["selected_backend"]
    
    def start_homescreen_process(self, _):
        self.device_paired = is_paired()

    def shutdown(self):
        """Cleanly shutdown the Skill removing any manual event handlers"""
        # Gotta clean up manually since not using add_event()
        self.bus.remove("ovos.pairing.process.completed", self.start_homescreen_process)
        self.bus.remove("ovos.pairing.set.backend", self.set_backend_type)

    #####################################################################
    # Device Settings

    @intent_handler("device.settings.intent")
    def handle_device_settings(self, message):
        """ Display device settings page. """
        self.gui["state"] = "settings/settingspage"
        self.gui.show_page("all.qml")

    @intent_handler("device.homescreen.settings.intent")
    def handle_device_homescreen_settings(self, message):
        """
        display homescreen settings page
        """
        #screens = [{"screenName": s, "screenID": self.resting_screen.screens[s]}
                   #for s in self.resting_screen.screens]
        #self.gui["idleScreenList"] = {"screenBlob": screens}
        #self.gui["selectedScreen"] = self.gui["selected"]
        self.gui["state"] = "settings/homescreen_settings"
        self.gui.show_page("all.qml")

    @intent_handler('device.ssh.settings.intent')
    def handle_device_ssh_settings(self, message):
        """ Display ssh settings page. """
        self.gui['state'] = 'settings/ssh_settings'
        self.gui.show_page('all.qml')
        
    def handle_device_developer_settings(self, message):
        """ Display developer settings page. """
        self.gui['state'] = 'settings/developer_settings'
        self.handle_device_dashboard_status_check()
        self.gui.show_page('all.qml')

    def handle_device_set_ssh(self, message):
        """ Set ssh settings """
        enable_ssh = message.data.get("enable_ssh", False)
        if enable_ssh:
            ssh_enable()
        elif not enable_ssh:
            ssh_disable()

    def handle_device_restart_action(self, message):
        """ Device restart action. """
        self.log.info("Going Down For Restart")
        system_reboot()

    def handle_device_poweroff_action(self, message):
        """ Device poweroff action. """
        self.log.info("Powering Off")
        system_shutdown()
        
    def handle_device_developer_enable_dash(self, message):
        self.log.info("Enabling Dashboard")
        os.environ["SIMPLELOGIN_USERNAME"] = "OVOS"
        os.environ["SIMPLELOGIN_PASSWORD"] = self.dash_secret
        build_call = "systemctl --user start ovos-dashboard@'{0}'.service".format(self.dash_secret)
        call_dash = subprocess.Popen([build_call], shell = True)
        time.sleep(3)
        build_status_check_call = "systemctl --user is-active --quiet ovos-dashboard@'{0}'.service".format(self.dash_secret)
        status = os.system(build_status_check_call)

        if status == 0:
            self.dash_running = True
        else:
            self.dash_running = False
        
        if self.dash_running:
            self.gui["dashboard_enabled"] = self.dash_running
            self.gui["dashboard_url"] = "https://{0}:5000".format(self._get_local_ip())
            self.gui["dashboard_user"] = "OVOS"
            self.gui["dashboard_password"] = self.dash_secret

    def handle_device_developer_disable_dash(self, message):
        self.log.info("Disabling Dashboard")
        build_call = "systemctl --user stop ovos-dashboard@'{0}'.service".format(self.dash_secret)
        subprocess.Popen([build_call], shell = True)
        time.sleep(3)
        build_status_check_call = "systemctl --user is-active --quiet ovos-dashboard@'{0}'.service".format(self.dash_secret)
        status = os.system(build_status_check_call)

        if status == 0:
            self.dash_running = True
        else:
            self.dash_running = False

        if not self.dash_running:
            self.gui["dashboard_enabled"] = self.dash_running
            self.gui["dashboard_url"] = ""
            self.gui["dashboard_user"] = ""
            self.gui["dashboard_password"] = ""

    def handle_device_dashboard_status_check(self):
        build_status_check_call = "systemctl --user is-active --quiet ovos-dashboard@'{0}'.service".format(self.dash_secret)
        status = os.system(build_status_check_call)

        self.log.info(self.dash_secret)
        self.log.info(status)

        if status == 0:
            self.dash_running = True
        else:
            self.dash_running = False

        if self.dash_running:
            self.gui["dashboard_enabled"] = self.dash_running
            self.gui["dashboard_url"] = "https://{0}:5000".format(self._get_local_ip())
            self.gui["dashboard_user"] = "OVOS"
            self.gui["dashboard_password"] = self.dash_secret

    #####################################################################
    # Helper Methods

    def _get_local_ip(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip

def create_skill():
    return OVOSGuiControlSkill()
