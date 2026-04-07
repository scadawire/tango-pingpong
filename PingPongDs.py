from time import time, sleep
from tango import AttrQuality, AttrWriteType, DispLevel, DevState, DevFloat, DevLong
from tango.server import Device, attribute, command, device_property
from tango.server import run
import tango
import threading
import os
from tango import EnsureOmniThread

class PingPongDs(Device):
    pong_device_name = device_property(dtype=str, default_value="")
    ping_interval_ms = device_property(dtype=int, default_value=0)

    # Attributes to track round trips
    total_roundtrips = 0
    avg_roundtrip_time = 0.0
    total_roundtrip_time = 0.0
    worst_roundtrip_time = 0.0
    best_roundtrip_time = 0.0
    last_roundtrip_time = 0.0
    connected = 0

    last_ping_time = 0.0  # To calculate roundtrip
    ping_tag = 0  # To tag each roundtrip uniquely
    pending_pings = {}  # Dictionary to store pending pings with tags
    t = 0
    start_time = 0

    def init_device(self):
        Device.init_device(self)
        self.set_state(DevState.INIT)
        self.get_device_properties(self.get_device_class())
        self.pong_device = None
        self.total_roundtrips = 0
        self.avg_roundtrip_time = 0.0
        self.total_roundtrip_time = 0.0
        self.worst_roundtrip_time = 0.0
        self.best_roundtrip_time = 0.0
        self.last_roundtrip_time = 0.0
        self.last_ping_time = 0.0
        self.ping_tag = 0
        self.pending_pings = {}
        self._lock = threading.Lock()
        self.last_tag = 0  # Initialize the print throttle time
        self.start_time = time()  # Record the start time
        self.reconnect()
        if(self.ping_interval_ms > 0):
            self.t = threading.Thread(target=self.ping_loop, daemon=True)
            self.t.start()
        # optimize lock contention
        util = tango.Util.instance()
        util.set_serial_model(tango.SerialModel.NO_SYNC)

        self.set_state(DevState.ON)

    def reconnect(self):
        # Start the pinging thread
        if self.pong_device_name:
            try:
                self.pong_device = tango.DeviceProxy(self.pong_device_name)
                self.info_stream(f"Successfully connected to {self.pong_device_name}")
                available_commands = self.pong_device.command_list_query()
                self.info_stream(f"Available commands on {self.pong_device_name}: {available_commands}")

                # This shows the actual connection details
                self.info_stream(f"CORBA IOR: {self.pong_device.dev_name()}")
                self.info_stream(f"Adm name: {self.pong_device.adm_name()}")

                self.connected = 1
            except Exception as e:
                self.error_stream(f"Failed to connect to pong device {self.pong_device_name}: {e}")

    def ping_loop(self):
        """Ping loop to regularly ping the other device."""
        sleepInterval = self.ping_interval_ms / 1000.0
        with EnsureOmniThread():
            while True:
                try:
                    self.trigger_ping()
                except Exception as e:
                    self.error_stream(f"Error in ping loop: {e}")
                sleep(sleepInterval)  # Convert ms to seconds

    def print_loop(self):
        while True:
            try:
                self.info_stream(f"Roundtrip #{self.last_tag} time: {round(self.last_roundtrip_time, 4)} ms, "
                             f"Total: {self.total_roundtrips}, "
                             f"Avg: {round(self.avg_roundtrip_time, 4)} ms, "
                             f"Worst: {round(self.worst_roundtrip_time, 4)} ms")
            except Exception as e:
                self.error_stream(f"Error in print loop: {e}")
            sleep(self.ping_interval_ms / 1000.0)  # Convert ms to seconds

    @command()
    def trigger_ping(self):
        """Send ping to the pong device."""
        if(self.connected == 0):
            self.reconnect()
        if self.pong_device is not None:
            self.ping_tag = (self.ping_tag + 1) & 0x7FFFFFFF  # wrap within DevLong range
            self.last_ping_time = time()
            # with self._lock:
            #     self.pending_pings[self.ping_tag] = self.last_ping_time
            self.pending_pings[self.ping_tag] = self.last_ping_time
            try:
                self.pong_device.pong(self.ping_tag)
            except Exception as e:
                # with self._lock:
                #     self.pending_pings.pop(self.ping_tag, None)
                self.pending_pings.pop(self.ping_tag, None)
                self.error_stream(f"Ping {self.ping_tag} failed: {e}")

    @command(dtype_in=DevLong)
    def ack(self, ping_tag):
        """Handle pong, calculate the roundtrip time using the provided tag."""
        with self._lock:
            send_time = self.pending_pings.pop(ping_tag, None)
        if send_time is not None:
            current_time = time()
            roundtrip_time = (current_time - send_time) * 1000.0  # Convert to ms

            # skip initial stats, since can be distorted
            if current_time - self.start_time < 5:
                return

            # Update metrics
            self.total_roundtrips += 1
            self.last_roundtrip_time = roundtrip_time
            self.total_roundtrip_time += roundtrip_time
            self.avg_roundtrip_time = self.total_roundtrip_time / self.total_roundtrips
            if roundtrip_time > self.worst_roundtrip_time:
                self.worst_roundtrip_time = roundtrip_time
            if roundtrip_time < self.best_roundtrip_time or self.best_roundtrip_time == 0:
                self.best_roundtrip_time = roundtrip_time
        else:
            self.error_stream(f"Received pong with unknown tag: {ping_tag}")

    @command(dtype_in=DevLong)
    def pong(self, ping_tag):
        if self.pong_device is not None:
            self.pong_device.ack(ping_tag)
        else:
            self.error_stream(f"Cannot send ack for tag {ping_tag}: not connected")

    @attribute(dtype=DevLong, unit="cnt")
    def totalRoundtrips(self):
        """Expose the total number of roundtrips."""
        return self.total_roundtrips

    @attribute(dtype=DevFloat, unit="ms", format="%8.4f")
    def avgRoundtripTime(self):
        """Expose the average roundtrip time in milliseconds."""
        return self.avg_roundtrip_time

    @attribute(dtype=DevFloat, unit="ms", format="%8.4f")
    def worstRoundtripTime(self):
        """Expose the worst roundtrip time in milliseconds."""
        return self.worst_roundtrip_time

    @attribute(dtype=DevFloat, unit="ms", format="%8.4f")
    def bestRoundtripTime(self):
        """Expose the best roundtrip time in milliseconds."""
        return self.best_roundtrip_time

    @attribute(dtype=DevFloat, unit="ms", format="%8.4f")
    def lastRoundtripTime(self):
        """Expose the last roundtrip time in milliseconds."""
        return self.last_roundtrip_time


if __name__ == "__main__":
    deviceServerName = os.getenv("DEVICE_SERVER_NAME", "PingPongDs")
    run({deviceServerName: PingPongDs})