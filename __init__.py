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
from datetime import datetime
import os
import subprocess
import secrets
import string
import socket
from threading import Thread, Lock

from pytz import timezone
import arrow
import astral
import pyaudio
from json_database import JsonStorage
from os.path import join, dirname, abspath

from mycroft.configuration.config import LocalConf, USER_CONFIG, Configuration
from mycroft.messagebus.message import Message
from mycroft.util import get_ipc_directory
from mycroft.util.log import LOG
from mycroft.util.parse import normalize
from mycroft import MycroftSkill, intent_handler
from ovos_utils.system import system_reboot, system_shutdown, ssh_enable, ssh_disable
from mycroft.api import DeviceApi, is_paired, check_remote_pairing


def compare_origin(msg1, msg2):
    """Compare the origin Skill of two Messages.

    Arguments:
        message (Message): Mycroft Message to compare
        message (Message): Mycroft Message to compare

    Returns:
        bool: Whether the Messages originate from the same Skill.
    """
    origin1 = msg1.data["__from"] if isinstance(msg1, Message) else msg1
    origin2 = msg2.data["__from"] if isinstance(msg2, Message) else msg2
    return origin1 == origin2


class RestingScreen:
    """Implementation of functionallity around resting screens.

    This class handles registration and override of resting screens,
    encapsulating the system.
    """

    def __init__(self, bus, gui, log, settings):
        self.bus = bus
        self.gui = gui
        self.log = log
        self.settings = settings

        self.screens = {}
        self.override_idle = None
        self.next = 0  # Next time the idle screen should trigger
        self.lock = Lock()
        self.override_set_time = time.monotonic()

        # Preselect OVOSHomescreen as resting screen
        self.gui["selected"] = self.settings.get("selected", "OVOSHomescreen")
        self.gui.set_on_gui_changed(self.save)

    def on_register(self, message):
        """Handler for catching incoming idle screens."""
        if "name" in message.data and "id" in message.data:
            self.screens[message.data["name"]] = message.data["id"]
            self.log.info("Registered {}".format(message.data["name"]))
        else:
            self.log.error("Malformed idle screen registration received")

    def save(self):
        """Handler to be called if the settings are changed by the GUI.

        Stores the selected idle screen.
        """
        self.log.debug("Saving resting screen")
        self.settings["selected"] = self.gui["selected"]
        self.gui["selectedScreen"] = self.gui["selected"]

    def collect(self):
        """Trigger collection and then show the resting screen."""
        self.bus.emit(Message("mycroft.mark2.collect_idle"))
        time.sleep(1)
        self.show()

    def set(self, message):
        """Set selected idle screen from message."""
        self.gui["selected"] = message.data["selected"]
        self.save()

    def show(self):
        """Show the idle screen or return to the skill that's overriding idle."""
        self.log.debug("Showing idle screen")
        screen = None
        if self.override_idle:
            self.log.debug("Returning to override idle screen")
            # Restore the page overriding idle instead of the normal idle
            self.bus.emit(self.override_idle[0])
        elif len(self.screens) > 0 and "selected" in self.gui:
            # TODO remove hard coded value
            self.log.info("Showing Idle screen for " "{}".format(self.gui["selected"]))
            screen = self.screens.get(self.gui["selected"])

        self.log.debug(screen)
        if screen:
            self.bus.emit(Message("{}.idle".format(screen)))

    def restore(self, _=None):
        """Remove any override and show the selected resting screen."""
        if self.override_idle and time.monotonic() - self.override_idle[1] > 2:
            self.override_idle = None
            self.show()

    def stop(self):
        if time.monotonic() > self.override_set_time + 7:
            self.restore()

    def force_stop(self):
        self.override_idle = None
        self.show()

    def override(self, message=None):
        """Override the resting screen.

        Arguments:
            message: Optional message to use for to restore
                     the expected override screen after
                     another screen has been displayed.
        """
        self.override_set_time = time.monotonic()
        if message:
            self.override_idle = (message, time.monotonic())

    def cancel_override(self):
        """Remove the override screen."""
        self.override_idle = None


class OVOSGuiControlSkill(MycroftSkill):
    """
    The OVOSGuiControl skill handles much of the gui activities related to Mycroft's
    core functionality. This includes showing "speaking" faces as well as
    more complicated things such as switching to the selected resting face
    and handling system signals.

    # TODO move most things to enclosure / HAL. Only voice interaction should
      reside in the Skill.
    """

    def __init__(self):
        super().__init__("OVOSGuiControl")

        self.settings["auto_brightness"] = False
        self.settings["use_listening_beep"] = True

        self.has_show_page = False  # resets with each handler
        self.override_animations = False
        self.resting_screen = None
        self.auto_brightness = None

        # Dashboard Specific
        self.dash_running = None
        alphabet = string.ascii_letters + string.digits
        self.dash_secret = ''.join(secrets.choice(alphabet) for i in range(5))

    def initialize(self):
        """Perform initalization.

        Registers messagebus handlers and sets default gui values.
        """
        self.resting_screen = RestingScreen(self.bus, self.gui, self.log, self.settings)

        self.brightness_dict = self.translate_namedvalues("brightness.levels")
        self.gui["mycroftgui"] = 0

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
            # Handle network connection events
            self.add_event("mycroft.internet.connected", self.handle_internet_connected)

            # Handle the 'busy' visual
            self.bus.on("mycroft.skill.handler.start", self.on_handler_started)

            self.bus.on("recognizer_loop:sleep", self.on_handler_sleep)
            self.bus.on("mycroft.awoken", self.on_handler_awoken)
            self.bus.on("enclosure.mouth.reset", self.on_handler_mouth_reset)
            self.bus.on("recognizer_loop:audio_output_end", self.on_handler_mouth_reset)
            self.bus.on("enclosure.mouth.viseme_list", self.on_handler_speaking)
            self.bus.on("gui.page.show", self.on_gui_page_show)
            self.bus.on("gui.page_interaction", self.on_gui_page_interaction)

            self.bus.on("mycroft.skills.initialized", self.reset_face)
            self.bus.on("ovos.pairing.process.completed", self.start_homescreen_process)
            self.bus.on("ovos.pairing.set.backend", self.set_backend_type)
            self.bus.on("mycroft.mark2.register_idle", self.resting_screen.on_register)

            self.add_event("mycroft.mark2.reset_idle", self.resting_screen.restore)
            # TODO move resting screen to Enclosure
            # TODO consolidate bus message format
            # - this message is set to be consistent with a handler below.
            self.add_event("mycroft.device.show.idle", self.resting_screen.show)

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
            self.gui.register_handler("mycroft.device.show.idle", 
                                      self.resting_screen.show)
            self.gui.register_handler("mycroft.device.settings.developer", self.handle_device_developer_settings)
            self.gui.register_handler("mycroft.device.enable.dash", self.handle_device_developer_enable_dash)
            self.gui.register_handler("mycroft.device.disable.dash", self.handle_device_developer_disable_dash)

            # Handle idle selection
            self.gui.register_handler("mycroft.device.set.idle", 
                                      self.resting_screen.set)

            # System events
            self.add_event("system.reboot",
                           self.handle_system_reboot)
            self.add_event("system.shutdown",
                           self.handle_system_shutdown)
            self.add_event("system.display.homescreen",
                           self.resting_screen.force_stop)

            # Show loading screen while starting up skills.
            # self.gui['state'] = 'loading'
            # self.gui.show_page('all.qml')

            # Collect Idle screens and display if skill is restarted
            self.device_paired = is_paired()
            self.device_backend = self.skill_conf["selected_backend"]

            if not self.device_backend == "local":
                if self.device_paired:
                    self.resting_screen.collect()
            else:
                self.resting_screen.collect()
                self.bus.emit(Message("ovos.shell.status.ok"))

        except Exception:
            LOG.exception("In OVOSGuiControl Skill")

        # Update use of wake-up beep
        self._sync_wake_beep_setting()

        self.settings_change_callback = self.on_websettings_changed

    ###################################################################
    # System events
    def handle_system_reboot(self, _):
        self.speak_dialog("rebooting", wait=True)
        system_reboot()

    def handle_system_shutdown(self, _):
        system_shutdown()
        
    def handle_remove_namespace(self, message):
        self.log.info("Got Clear Namespace Event In Mark 2 Skill")
        get_skill_namespace = message.data.get("skill_id", "")
        if get_skill_namespace:
            self.bus.emit(Message("gui.clear.namespace",
                                  {"__from": get_skill_namespace}))
        self.resting_screen.cancel_override()
        self.cancel_scheduled_event("IdleCheck")

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
        self.resting_screen.collect()
        
    def reset_face(self, _):
        """Triggered after skills are initialized.

        Sets switches from resting "face" to a registered resting screen.
        """
        time.sleep(1)
        if self.device_paired or self.device_backend == "local":
            self.resting_screen.collect()

    def stop(self, _=None):
        """Clear override_idle and stop visemes."""
        self.log.debug("Stop received")
        self.resting_screen.stop()
        self.gui["viseme"] = {"start": 0, "visemes": []}
        return False

    def shutdown(self):
        """Cleanly shutdown the Skill removing any manual event handlers"""
        # Gotta clean up manually since not using add_event()
        self.bus.remove("mycroft.skill.handler.start", self.on_handler_started)
        self.bus.remove("recognizer_loop:sleep", self.on_handler_sleep)
        self.bus.remove("mycroft.awoken", self.on_handler_awoken)
        self.bus.remove("enclosure.mouth.reset", self.on_handler_mouth_reset)
        self.bus.remove("recognizer_loop:audio_output_end", self.on_handler_mouth_reset)
        self.bus.remove("enclosure.mouth.viseme_list", self.on_handler_speaking)
        self.bus.remove("gui.page.show", self.on_gui_page_show)
        self.bus.remove("gui.page_interaction", self.on_gui_page_interaction)
        self.bus.remove("mycroft.mark2.register_idle", self.resting_screen.on_register)
        self.bus.remove("ovos.pairing.process.completed", self.start_homescreen_process)
        self.bus.remove("ovos.pairing.set.backend", self.set_backend_type)

    #####################################################################
    # Manage "busy" visual

    def on_handler_started(self, message):
        handler = message.data.get("handler", "")
        # Ignoring handlers from this skill and from the background clock
        if "OVOSGuiControl" in handler:
            return
        if "TimeSkill.update_display" in handler:
            return

    def on_gui_page_interaction(self, _):
        """ Reset idle timer to 30 seconds when page is flipped. """
        self.log.debug("Resetting idle counter to 30 seconds")
        self.start_idle_event(30)

    def on_gui_page_show(self, message):
        self.log.info(message.data.get("__from", ""))
        if "skill-ovos-mycroftgui" not in message.data.get("__from", ""):
            # Some skill other than the handler is showing a page
            self.has_show_page = True

            # If a skill overrides the animations do not show any
            override_animations = message.data.get("__animations", False)
            if override_animations:
                # Disable animations
                self.log.debug("Disabling all animations for page")
                self.override_animations = True
            else:
                self.log.debug("Displaying all animations for page")
                self.override_animations = False

            # If a skill overrides the idle do not switch page
            override_idle = message.data.get("__idle")
            if override_idle is True:
                # Disable idle screen
                self.log.debug("Cancelling Idle screen")
                self.cancel_idle_event()
                self.resting_screen.override(message)
            elif isinstance(override_idle, int) and override_idle is not False:
                self.log.info(
                    "Overriding idle timer to" " {} seconds".format(override_idle)
                )
                self.resting_screen.override(None)
                self.start_idle_event(override_idle)
            elif message.data["page"] and not message.data["page"][0].endswith(
                "idle.qml"
            ):
                # Check if the idle override has been set and if this call of
                # show_page should deactivate a previous idle override
                # This is only possible if the page is from the same skill
                self.log.info("Cancelling idle override")
                if self.resting_screen.override_idle is not None and \
                    override_idle is False and \
                    compare_origin(message,
                                    self.resting_screen.override_idle[0]):
                    # Remove the idle override page if override is set to false
                    self.resting_screen.cancel_override()
                # Set default idle screen timer
                self.start_idle_event(30)

    def on_handler_mouth_reset(self, _):
        """ Restore viseme to a smile. """
        pass

    def on_handler_sleep(self, _):
        """ Show resting face when going to sleep. """
        self.gui["state"] = "resting"
        self.gui.show_page("all.qml")

    def on_handler_awoken(self, _):
        """ Show awake face when sleep ends. """
        self.gui["state"] = "awake"
        self.gui.show_page("all.qml")

    def on_handler_complete(self, message):
        """ When a skill finishes executing clear the showing page state. """
        handler = message.data.get("handler", "")
        # Ignoring handlers from this skill and from the background clock
        if "OVOSGuiControl" in handler:
            return
        if "TimeSkill.update_display" in handler:
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
        """Show the speaking page if no skill has registered a page
        to be shown in it's place.
        """
        if self.device_paired or self.device_backend == "local":
            self.gui["viseme"] = message.data
            if not self.has_show_page:
                self.gui["state"] = "speaking"
                self.gui.show_page("all.qml")
                # Show idle screen after the visemes are done (+ 2 sec).
                viseme_time = message.data["visemes"][-1][1] + 5
                self.start_idle_event(viseme_time)

    #####################################################################
    # Manage resting screen visual state
    def cancel_idle_event(self):
        """Cancel the event monitoring current system idle time."""
        self.resting_screen.next = 0
        self.cancel_scheduled_event("IdleCheck")

    def start_idle_event(self, offset=60, weak=False):
        """Start an event for showing the idle screen.

        Arguments:
            offset: How long until the idle screen should be shown
            weak: set to true if the time should be able to be overridden
        """
        with self.resting_screen.lock:
            if time.monotonic() + offset < self.resting_screen.next:
                self.log.info("No update, before next time")
                return

            self.log.debug("Starting idle event")
            try:
                if not weak:
                    self.resting_screen.next = time.monotonic() + offset
                # Clear any existing checker
                self.cancel_scheduled_event("IdleCheck")
                time.sleep(0.5)
                self.schedule_event(
                    self.resting_screen.show, int(offset), name="IdleCheck"
                )
                self.log.debug("Showing idle screen in " "{} seconds".format(offset))
            except Exception as e:
                self.log.exception(repr(e))

    #####################################################################
    # Manage network

    def handle_internet_connected(self, _):
        """ System came online later after booting. """
        self.enclosure.mouth_reset()

    #####################################################################
    # Web settings

    def on_websettings_changed(self):
        """ Update use of wake-up beep. """
        self._sync_wake_beep_setting()

    def _sync_wake_beep_setting(self):
        """ Update "use beep" global config from skill settings. """
        config = Configuration.get()
        use_beep = self.settings.get("use_listening_beep", False)
        if not config["confirm_listening"] == use_beep:
            # Update local (user) configuration setting
            new_config = {"confirm_listening": use_beep}
            user_config = LocalConf(USER_CONFIG)
            user_config.merge(new_config)
            user_config.store()
            self.bus.emit(Message("configuration.updated"))

    #####################################################################
    # Brightness intent interaction

    def percent_to_level(self, percent):
        """Converts the brigtness value from percentage to a
        value the Arduino can read

        Arguments:
            percent (int): interger value from 0 to 100

        return:
            (int): value form 0 to 30
        """
        return int(float(percent) / float(100) * 30)

    def parse_brightness(self, brightness):
        """Parse text for brightness percentage.

        Arguments:
            brightness (str): string containing brightness level

        Returns:
            (int): brightness as percentage (0-100)
        """

        try:
            # Handle "full", etc.
            name = normalize(brightness)
            if name in self.brightness_dict:
                return self.brightness_dict[name]

            if "%" in brightness:
                brightness = brightness.replace("%", "").strip()
                return int(brightness)
            if "percent" in brightness:
                brightness = brightness.replace("percent", "").strip()
                return int(brightness)

            i = int(brightness)
            if i < 0 or i > 100:
                return None

            if i < 30:
                # Assmume plain 0-30 is "level"
                return int((i * 100.0) / 30.0)

            # Assume plain 31-100 is "percentage"
            return i
        except Exception:
            return None  # failed in an int() conversion

    def set_screen_brightness(self, level, speak=True):
        """Actually change screen brightness.

        Arguments:
            level (int): 0-30, brightness level
            speak (bool): when True, speak a confirmation
        """
        # TODO CHANGE THE BRIGHTNESS
        if speak:
            percent = int(float(level) * float(100) / float(30))
            self.speak_dialog("brightness.set", data={"val": str(percent) + "%"})

    def _set_brightness(self, brightness):
        # brightness can be a number or word like "full", "half"
        percent = self.parse_brightness(brightness)
        if percent is None:
            self.speak_dialog("brightness.not.found.final")
        elif int(percent) == -1:
            self.handle_auto_brightness(None)
        else:
            self.auto_brightness = False
            self.set_screen_brightness(self.percent_to_level(percent))

    @intent_handler("brightness.intent")
    def handle_brightness(self, message):
        """Intent handler to set custom screen brightness.

        Arguments:
            message (dict): messagebus message from intent parser
        """
        brightness = message.data.get("brightness", None) or self.get_response(
            "brightness.not.found"
        )
        if brightness:
            self._set_brightness(brightness)

    def _get_auto_time(self):
        """Get dawn, sunrise, noon, sunset, and dusk time.

        Returns:
            times (dict): dict with associated (datetime, level)
        """
        tz_code = self.location["timezone"]["code"]
        lat = self.location["coordinate"]["latitude"]
        lon = self.location["coordinate"]["longitude"]
        ast_loc = astral.Location()
        ast_loc.timezone = tz_code
        ast_loc.lattitude = lat
        ast_loc.longitude = lon

        user_set_tz = timezone(tz_code).localize(datetime.now()).strftime("%Z")
        device_tz = time.tzname

        if user_set_tz in device_tz:
            sunrise = ast_loc.sun()["sunrise"]
            noon = ast_loc.sun()["noon"]
            sunset = ast_loc.sun()["sunset"]
        else:
            secs = int(self.location["timezone"]["offset"]) / -1000
            sunrise = (
                arrow.get(ast_loc.sun()["sunrise"])
                .shift(seconds=secs)
                .replace(tzinfo="UTC")
                .datetime
            )
            noon = (
                arrow.get(ast_loc.sun()["noon"])
                .shift(seconds=secs)
                .replace(tzinfo="UTC")
                .datetime
            )
            sunset = (
                arrow.get(ast_loc.sun()["sunset"])
                .shift(seconds=secs)
                .replace(tzinfo="UTC")
                .datetime
            )

        return {
            "Sunrise": (sunrise, 20),  # high
            "Noon": (noon, 30),  # full
            "Sunset": (sunset, 5),  # dim
        }

    def schedule_brightness(self, time_of_day, pair):
        """Schedule auto brightness with the event scheduler.

        Arguments:
            time_of_day (str): Sunrise, Noon, Sunset
            pair (tuple): (datetime, brightness)
        """
        d_time = pair[0]
        brightness = pair[1]
        now = arrow.now()
        arw_d_time = arrow.get(d_time)
        data = (time_of_day, brightness)
        if now.timestamp > arw_d_time.timestamp:
            d_time = arrow.get(d_time).shift(hours=+24)
            self.schedule_event(
                self._handle_screen_brightness_event,
                d_time,
                data=data,
                name=time_of_day,
            )
        else:
            self.schedule_event(
                self._handle_screen_brightness_event,
                d_time,
                data=data,
                name=time_of_day,
            )

    @intent_handler("brightness.auto.intent")
    def handle_auto_brightness(self, _):
        """brightness varies depending on time of day

        Arguments:
            message (Message): messagebus message from intent parser
        """
        self.auto_brightness = True
        auto_time = self._get_auto_time()
        nearest_time_to_now = (float("inf"), None, None)
        for time_of_day, pair in auto_time.items():
            self.schedule_brightness(time_of_day, pair)
            now = arrow.now().timestamp
            timestamp = arrow.get(pair[0]).timestamp
            if abs(now - timestamp) < nearest_time_to_now[0]:
                nearest_time_to_now = (abs(now - timestamp), pair[1], time_of_day)
        self.set_screen_brightness(nearest_time_to_now[1], speak=False)

    def _handle_screen_brightness_event(self, message):
        """Wrapper for setting screen brightness from eventscheduler

        Arguments:
            message (Message): messagebus message
        """
        if self.auto_brightness:
            time_of_day = message.data[0]
            level = message.data[1]
            self.cancel_scheduled_event(time_of_day)
            self.set_screen_brightness(level, speak=False)
            pair = self._get_auto_time()[time_of_day]
            self.schedule_brightness(time_of_day, pair)

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
        screens = [{"screenName": s, "screenID": self.resting_screen.screens[s]}
                   for s in self.resting_screen.screens]
        self.gui["idleScreenList"] = {"screenBlob": screens}
        self.gui["selectedScreen"] = self.gui["selected"]
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
