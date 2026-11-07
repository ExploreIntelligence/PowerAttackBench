from scada_core.real_iec104.bridge_runtime import interpret_native_command


def test_interpret_native_command_trip_line():
    # IOA = 100000 + from*128 + to: trip 1-5 -> 100133
    result = interpret_native_command("[server] process command IOA=100133 command=1")
    assert result == {"action": "trip_line", "from_bus": 1, "to_bus": 5}


def test_interpret_native_command_close_line():
    # IOA = 200000 + from*128 + to: close 5-6 -> 200646
    result = interpret_native_command("[server] process command IOA=200646 command=1")
    assert result == {"action": "close_line", "from_bus": 5, "to_bus": 6}


def test_interpret_native_command_three_digit_bus():
    # Base 128 must decode three-digit buses without carry ambiguity (118*128+76).
    result = interpret_native_command("[server] process command IOA=115180 command=1")
    assert result == {"action": "trip_line", "from_bus": 118, "to_bus": 76}


def test_interpret_native_command_rejects_fixed_demo_ioa():
    try:
        interpret_native_command("[server] process command IOA=4001 command=1")
    except ValueError:
        pass
    else:
        raise AssertionError("fixed demonstration IOA must be rejected")
