from time import time, sleep
from tango import AttrQuality, AttrWriteType, DispLevel, DevState, DevFloat, DevLong
from tango.server import Device, attribute, command, device_property
from tango.server import run
import tango
import threading
import os

class PingPongDs(Device):
    pong_device_name = device_property(dtype=str, default_value="")
    ping_interval_ms = device_property(dtype=int, default_value=0)

    # Attributes to track round trips
    total_roundtrips = 0
    avg_roundtrip_time = 0.0
    worst_roundtrip_time = 0.0
    connected = 0

    last_ping_time = 0.0  # To calculate roundtrip
    ping_tag = 0  # To tag each roundtrip uniquely
    pending_pings = {}  # Dictionary to store pending pings with tags
    t = 0
    last_print_time = 0
    start_time = 0

    def init_device(self):
        Device.init_device(self)
        self.set_state(DevState.INIT)
        self.get_device_properties(self.get_device_class())
        self.pong_device = None
        self.total_roundtrips = 0
        self.avg_roundtrip_time = 0.0
        self.worst_roundtrip_time = 0.0
        self.last_ping_time = 0.0
        self.ping_tag = 0
        self.pending_pings = {}
        self.last_print_time = time()  # Initialize the print throttle time
        self.start_time = time()  # Record the start time
        self.reconnect()
        if(self.ping_interval_ms > 0):
            self.t = threading.Thread(target=self.ping_loop)
            self.t.start()
        self.set_state(DevState.ON)

    def reconnect(self):
        # Start the pinging thread
        if self.pong_device_name:
            try:
                self.pong_device = tango.DeviceProxy(self.pong_device_name)
                self.info_stream(f"Successfully connected to {self.pong_device_name}")
                available_commands = self.pong_device.command_list_query()
                self.info_stream(f"Available commands on {self.pong_device_name}: {available_commands}")
                self.connected = 1
            except Exception as e:
                self.error_stream(f"Failed to connect to pong device {self.pong_device_name}: {e}")

    def ping_loop(self):
        """Ping loop to regularly ping the other device."""
        while True:
            try:
                self.trigger_ping()
            except Exception as e:
                self.error_stream(f"Error in ping loop: {e}")
            sleep(self.ping_interval_ms / 1000.0)  # Convert ms to seconds

    @command()
    def trigger_ping(self):
        """Send ping to the pong device."""
        if(self.connected == 0):
            self.reconnect()
        if self.pong_device is not None:
            self.ping_tag += 1  # Increment tag to uniquely identify this ping
            self.last_ping_time = time()
            self.pending_pings[self.ping_tag] = self.last_ping_time  # Store ping time for this tag
            self.pong_device.pong(self.ping_tag)  # Send tag to the other device

    @command(dtype_in=DevLong)
    def ack(self, ping_tag):
        """Handle pong, calculate the roundtrip time using the provided tag."""
        if ping_tag in self.pending_pings:
            roundtrip_time = (time() - self.pending_pings[ping_tag]) * 1000.0  # Convert to ms
            del self.pending_pings[ping_tag]  # Remove the completed ping from the dictionary

            # skip initial stats, since can be distorted
            if time() - self.start_time < 5:
                return

            # Update metrics
            self.total_roundtrips += 1
            self.avg_roundtrip_time = ((self.avg_roundtrip_time * (self.total_roundtrips - 1)) + roundtrip_time) / self.total_roundtrips
            if roundtrip_time > self.worst_roundtrip_time:
                self.worst_roundtrip_time = roundtrip_time

            current_time = time()
            if current_time - self.last_print_time >= 1.0:  # Throttle output to 1 second
                self.last_print_time = current_time
                self.info_stream(f"Roundtrip {ping_tag} time: {round(roundtrip_time, 4)} ms, "
                                 f"Total: {self.total_roundtrips}, "
                                 f"Avg: {round(self.avg_roundtrip_time, 4)} ms, "
                                 f"Worst: {round(self.worst_roundtrip_time, 4)} ms")
        else:
            self.error_stream(f"Received pong with unknown tag: {ping_tag}")

    @command(dtype_in=DevLong)
    def pong(self, ping_tag):
        self.pong_device.ack(ping_tag)  # Send back

    @attribute(dtype=DevLong)
    def totalRoundtrips(self):
        """Expose the total number of roundtrips."""
        return self.total_roundtrips

    @attribute(dtype=DevFloat)
    def avgRoundtripTime(self):
        """Expose the average roundtrip time in milliseconds."""
        return self.avg_roundtrip_time

    @attribute(dtype=DevFloat)
    def worstRoundtripTime(self):
        """Expose the worst roundtrip time in milliseconds."""
        return self.worst_roundtrip_time


if __name__ == "__main__":
    deviceServerName = os.getenv("DEVICE_SERVER_NAME", "PingPongDs")
    run({deviceServerName: PingPongDs})