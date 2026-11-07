from __future__ import annotations

import cmd

from .power_model import AREA_LAYOUT, PowerFlowEngine
from .scada import SCADAController
from .attacker import AttackerClient


class SCADAConsole(cmd.Cmd):
    intro = "SCADA Center controller. Commands: connect, disconnect, trip_line, close_line, set_load, branch, bus, generator, exit"
    prompt = "scada> "

    def __init__(self):
        super().__init__()
        self.engine = PowerFlowEngine()
        self.scada = SCADAController(self.engine)
        self.attacker = AttackerClient(self.scada)

    def do_connect(self, arg):
        print(self.scada.connect())

    def do_disconnect(self, arg):
        print(self.scada.disconnect())

    def do_trip_line(self, arg):
        args = arg.split()
        if len(args) == 2:
            from_bus, to_bus = (int(value) for value in args)
            area = next(area for area, buses in AREA_LAYOUT.items() if from_bus in buses)
        elif len(args) == 3:
            area, from_bus, to_bus = args
            from_bus, to_bus = int(from_bus), int(to_bus)
        else:
            print("Usage: trip_line <from_bus> <to_bus>")
            return
        print(self.scada.trip_line(area, from_bus, to_bus))

    def do_close_line(self, arg):
        args = arg.split()
        if len(args) == 2:
            from_bus, to_bus = (int(value) for value in args)
            area = next(area for area, buses in AREA_LAYOUT.items() if from_bus in buses)
        elif len(args) == 3:
            area, from_bus, to_bus = args
            from_bus, to_bus = int(from_bus), int(to_bus)
        else:
            print("Usage: close_line <from_bus> <to_bus>")
            return
        print(self.scada.close_line(area, from_bus, to_bus))

    def do_set_load(self, arg):
        args = arg.split()
        if len(args) != 4:
            print("Usage: set_load <area> <bus_id> <pd> <qd>")
            return
        area, bus_id, pd, qd = args
        print(self.scada.set_load(area, int(bus_id), float(pd), float(qd)))

    def do_branch(self, arg):
        print(self.scada.branch())

    def do_bus(self, arg):
        print(self.scada.bus())

    def do_generator(self, arg):
        print(self.scada.generator())

    def do_attacker(self, arg):
        self.attacker.connect()
        print("Attacker connected to SCADA center")

    def do_exit(self, arg):
        return True

    def do_EOF(self, arg):
        return True


if __name__ == "__main__":
    SCADAConsole().cmdloop()
