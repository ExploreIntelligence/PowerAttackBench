#include <signal.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#include "cs104_slave.h"
#include "cs101_information_objects.h"
#include "hal_thread.h"
#include "hal_time.h"

/* Command IOA layout: trip 100000 + from*BASE + to, close 200000 + ...
 * BASE must exceed the largest bus number of every supported case
 * (128 supports up to bus 127; the largest current case is IEEE-118). */
#define CMD_SEGMENT_TRIP   100000
#define CMD_SEGMENT_CLOSE  200000
#define CMD_SEGMENT_PD     300000
#define CMD_SEGMENT_QD     400000
#define CMD_SEGMENT_SIZE   100000
#define CMD_BUS_BASE       128

static bool running = true;
static int lastCommandValue = 0;
static int lastCommandIoa = 0;
static int pendingLoadBus = 0;
static float pendingLoadPd = 0.0f;
static const char* commandPath = "/tmp/commands.jsonl";
static const char* statePath = "/tmp/state.json";
static int monitoredBuses[256];
static int monitoredBusCount = 0;

/* Control-command rate limiting: at most CMD_RATE_LIMIT commands per fixed
 * 60-second window per area. 0 (default) = unlimited. Telemetry (GI and
 * periodic measurements) is not throttled. */
static int rateLimitPerWindow = 0;
static time_t windowStart = 0;
static int windowCount = 0;

static bool rate_limit_ok(void)
{
    time_t now;
    if (rateLimitPerWindow <= 0)
        return true;
    now = time(NULL);
    if (windowStart == 0 || now - windowStart >= 60) {
        windowStart = now;
        windowCount = 0;
    }
    if (windowCount >= rateLimitPerWindow)
        return false;
    windowCount++;
    return true;
}

static double read_state_number(const char* key, double fallback)
{
    FILE* file = fopen(statePath, "r");
    char buffer[16384];
    char* match;
    double value;

    if (!file)
        return fallback;
    size_t length = fread(buffer, 1, sizeof(buffer) - 1, file);
    fclose(file);
    buffer[length] = '\0';
    match = strstr(buffer, key);
    if (!match || sscanf(match + strlen(key), " %*[: ] %lf", &value) != 1)
        return fallback;
    return value;
}

/* Grid collapse latch published by the bridge in state.json. Once the power
 * flow has failed to converge, telemetry keeps flowing (frozen values with
 * QDS, physical zeros) but every control point must refuse commands with a
 * negative ACT_CON. A missing/older state file fails open (not collapsed). */
static bool system_collapsed(void)
{
    FILE* file = fopen(statePath, "r");
    char buffer[65536];
    size_t length;

    if (!file)
        return false;
    length = fread(buffer, 1, sizeof(buffer) - 1, file);
    fclose(file);
    buffer[length] = '\0';
    /* Python json.dumps emits ": " by default; tolerate the compact form too. */
    return strstr(buffer, "\"collapsed\": true") != NULL
        || strstr(buffer, "\"collapsed\":true") != NULL;
}

static void add_bus_measurements(CS101_ASDU asdu, int bus)
{
    char key[32];
    InformationObject object;
    int quality;

    /* Per-bus QDS from the bridge state: 0 = GOOD, 0x80 = IV (invalid/stale).
     * Missing key (older state files) defaults to GOOD. */
    snprintf(key, sizeof(key), "\"bus_%d_q\"", bus);
    quality = (int) read_state_number(key, IEC60870_QUALITY_GOOD);

    snprintf(key, sizeof(key), "\"bus_%d_vm\"", bus);
    object = (InformationObject) MeasuredValueScaled_create(NULL, 1000 + bus,
        (int) read_state_number(key, 0), quality);
    CS101_ASDU_addInformationObject(asdu, object);
    InformationObject_destroy(object);

    snprintf(key, sizeof(key), "\"bus_%d_pd\"", bus);
    object = (InformationObject) MeasuredValueScaled_create(NULL, 2000 + bus,
        (int) read_state_number(key, 0), quality);
    CS101_ASDU_addInformationObject(asdu, object);
    InformationObject_destroy(object);

    snprintf(key, sizeof(key), "\"bus_%d_qd\"", bus);
    object = (InformationObject) MeasuredValueScaled_create(NULL, 3000 + bus,
        (int) read_state_number(key, 0), quality);
    CS101_ASDU_addInformationObject(asdu, object);
    InformationObject_destroy(object);

    snprintf(key, sizeof(key), "\"bus_%d_pg\"", bus);
    object = (InformationObject) MeasuredValueScaled_create(NULL, 4000 + bus,
        (int) read_state_number(key, 0), quality);
    CS101_ASDU_addInformationObject(asdu, object);
    InformationObject_destroy(object);
}

static void add_all_bus_measurements(CS101_ASDU asdu)
{
    for (int i = 0; i < monitoredBusCount; i++)
        add_bus_measurements(asdu, monitoredBuses[i]);
}

static bool bus_in_area(int bus)
{
    for (int i = 0; i < monitoredBusCount; i++)
        if (monitoredBuses[i] == bus)
            return true;
    return false;
}

/* Commissioned point table: directed branch pairs ("from-to") loaded from
 * the POWER_LINES environment variable, mirroring the PYPOWER case branch
 * matrix exactly (same direction, parallel branches collapsed). Like a real
 * device, a controller only accepts commands for points that exist in its
 * commissioned database; unknown points get a negative ACT_CON with
 * COT=47 UNKNOWN_IOA. An empty/missing table fails closed: no controllable
 * points, every trip/close rejected. */
static int branchFrom[512];
static int branchTo[512];
static int branchCount = 0;

static bool branch_exists(int from_bus, int to_bus)
{
    for (int i = 0; i < branchCount; i++)
        if (branchFrom[i] == from_bus && branchTo[i] == to_bus)
            return true;
    return false;
}

/* Decode the from_bus of a trip (100000+) / close (200000+) command IOA.
 * Returns -1 when the IOA is outside both command ranges. */
static int command_from_bus(int ioa)
{
    if (ioa >= CMD_SEGMENT_TRIP && ioa < CMD_SEGMENT_TRIP + CMD_SEGMENT_SIZE)
        return (ioa - CMD_SEGMENT_TRIP) / CMD_BUS_BASE;
    if (ioa >= CMD_SEGMENT_CLOSE && ioa < CMD_SEGMENT_CLOSE + CMD_SEGMENT_SIZE)
        return (ioa - CMD_SEGMENT_CLOSE) / CMD_BUS_BASE;
    return -1;
}

/* Decode the to_bus of a trip/close command IOA. Only meaningful when
 * command_from_bus() returned a valid segment. */
static int command_to_bus(int ioa)
{
    if (ioa >= CMD_SEGMENT_TRIP && ioa < CMD_SEGMENT_TRIP + CMD_SEGMENT_SIZE)
        return (ioa - CMD_SEGMENT_TRIP) % CMD_BUS_BASE;
    return (ioa - CMD_SEGMENT_CLOSE) % CMD_BUS_BASE;
}

/* Decode the bus of a setpoint (300000+/400000+) IOA. Returns -1 otherwise. */
static int setpoint_bus(int ioa)
{
    if (ioa >= CMD_SEGMENT_PD && ioa < CMD_SEGMENT_PD + CMD_SEGMENT_SIZE)
        return ioa - CMD_SEGMENT_PD;
    if (ioa >= CMD_SEGMENT_QD && ioa < CMD_SEGMENT_QD + CMD_SEGMENT_SIZE)
        return ioa - CMD_SEGMENT_QD;
    return -1;
}

static void append_command(int ioa, int value)
{
    FILE* file = fopen(commandPath, "a");
    if (!file)
        return;
    if (ioa >= CMD_SEGMENT_TRIP && ioa < CMD_SEGMENT_TRIP + CMD_SEGMENT_SIZE)
        fprintf(file, "{\"action\":\"trip_line\",\"from_bus\":%d,\"to_bus\":%d}\n",
                (ioa - CMD_SEGMENT_TRIP) / CMD_BUS_BASE,
                (ioa - CMD_SEGMENT_TRIP) % CMD_BUS_BASE);
    else if (ioa >= CMD_SEGMENT_CLOSE && ioa < CMD_SEGMENT_CLOSE + CMD_SEGMENT_SIZE)
        fprintf(file, "{\"action\":\"close_line\",\"from_bus\":%d,\"to_bus\":%d}\n",
                (ioa - CMD_SEGMENT_CLOSE) / CMD_BUS_BASE,
                (ioa - CMD_SEGMENT_CLOSE) % CMD_BUS_BASE);
    else
        fprintf(file, "{\"action\":\"unknown\",\"ioa\":%d,\"value\":%d}\n", ioa, value);
    fclose(file);
}

static void sigint_handler(int signalId)
{
    (void)signalId;
    running = false;
}

static void
connectionEventHandler(void* parameter, IMasterConnection con, CS104_PeerConnectionEvent event)
{
    (void)parameter;
    (void)con;
    if (event == CS104_CON_EVENT_CONNECTION_OPENED)
        printf("[server] connection opened\n");
    else if (event == CS104_CON_EVENT_CONNECTION_CLOSED)
        printf("[server] connection closed\n");
}

/* Negative command response: ACT_CON with the P/N bit set, optionally with a
 * standard diagnostic cause (44-47) instead of plain COT=7. Each enqueue
 * serializes an immediate copy, so the same ASDU can be reused afterwards. */
static void
send_negative_con(IMasterConnection con, CS101_ASDU asdu, CS101_CauseOfTransmission cause)
{
    CS101_ASDU_setCOT(asdu, cause);
    CS101_ASDU_setNegative(asdu, true);
    IMasterConnection_sendASDU(con, asdu);
}

static bool
asduHandler(void* parameter, IMasterConnection con, CS101_ASDU asdu)
{
    (void)parameter;

    int typeId = CS101_ASDU_getTypeID(asdu);
    printf("[server] received ASDU type=%d elements=%d\n", typeId, CS101_ASDU_getNumberOfElements(asdu));

    if (typeId == C_SC_NA_1) {
        SingleCommand sc = (SingleCommand) CS101_ASDU_getElement(asdu, 0);
        if (!sc) {
            send_negative_con(con, asdu, CS101_COT_ACTIVATION_CON);
            return true;
        }
        int ioa = InformationObject_getObjectAddress((InformationObject) sc);
        int value = SingleCommand_getState(sc);
        SingleCommand_destroy(sc);

        if (!rate_limit_ok()) {
            printf("[server] rate limit exceeded, rejected IOA=%d\n", ioa);
            send_negative_con(con, asdu, CS101_COT_ACTIVATION_CON);
            return true;
        }
        int from_bus = command_from_bus(ioa);
        if (from_bus < 0) {
            /* Recognized command type, unknown information object address. */
            printf("[server] rejected IOA=%d unknown command address\n", ioa);
            send_negative_con(con, asdu, CS101_COT_UNKNOWN_IOA);
            return true;
        }
        if (!bus_in_area(from_bus)) {
            printf("[server] rejected IOA=%d from_bus=%d not in this area\n", ioa, from_bus);
            send_negative_con(con, asdu, CS101_COT_ACTIVATION_CON);
            return true;
        }
        if (!branch_exists(from_bus, command_to_bus(ioa))) {
            /* Point not in the commissioned point table: the IOA does not
             * decode to any existing line, so the device never owned this
             * control point. Standard negative confirmation, and the
             * command is not logged (no state change can occur). */
            printf("[server] rejected IOA=%d line %d-%d not in point table\n",
                ioa, from_bus, command_to_bus(ioa));
            send_negative_con(con, asdu, CS101_COT_UNKNOWN_IOA);
            return true;
        }
        if (system_collapsed()) {
            /* The grid has latched in its collapsed state: the point is valid
             * but the process cannot be actuated anymore. Standard negative
             * ACT_CON (P/N=1, COT=7), no ACT_TERM, command not logged. */
            printf("[server] rejected IOA=%d system collapsed\n", ioa);
            send_negative_con(con, asdu, CS101_COT_ACTIVATION_CON);
            return true;
        }
        /* Standard command procedure: positive ACT_CON before execution, then
         * ACT_TERM once the command has been carried out. */
        IMasterConnection_sendACT_CON(con, asdu, false);
        lastCommandIoa = ioa;
        lastCommandValue = value;
        append_command(ioa, value);
        printf("[server] process command IOA=%d command=%d\n", ioa, value);
        IMasterConnection_sendACT_TERM(con, asdu);
        return true;
    }

    if (typeId == C_SE_NC_1) {
        if (!rate_limit_ok()) {
            printf("[server] rate limit exceeded, rejected setpoint ASDU\n");
            send_negative_con(con, asdu, CS101_COT_ACTIVATION_CON);
            return true;
        }
        float pd = 0.0f;
        float qd = 0.0f;
        int pd_bus = 0;
        int qd_bus = 0;
        int elements = CS101_ASDU_getNumberOfElements(asdu);
        if (elements < 1) {
            send_negative_con(con, asdu, CS101_COT_ACTIVATION_CON);
            return true;
        }
        for (int i = 0; i < elements; i++) {
            SetpointCommandShort sp = (SetpointCommandShort) CS101_ASDU_getElement(asdu, i);
            if (!sp) {
                send_negative_con(con, asdu, CS101_COT_ACTIVATION_CON);
                return true;
            }
            int ioa = InformationObject_getObjectAddress((InformationObject) sp);
            int sp_bus = setpoint_bus(ioa);
            float sp_value = SetpointCommandShort_getValue(sp);
            SetpointCommandShort_destroy(sp);
            if (sp_bus < 0) {
                printf("[server] rejected setpoint IOA=%d unknown address\n", ioa);
                send_negative_con(con, asdu, CS101_COT_UNKNOWN_IOA);
                return true;
            }
            if (!bus_in_area(sp_bus)) {
                printf("[server] rejected setpoint IOA=%d bus=%d not in this area\n", ioa, sp_bus);
                send_negative_con(con, asdu, CS101_COT_ACTIVATION_CON);
                return true;
            }
            if (ioa >= CMD_SEGMENT_PD && ioa < CMD_SEGMENT_PD + CMD_SEGMENT_SIZE) {
                pd_bus = ioa - CMD_SEGMENT_PD;
                pd = sp_value;
            } else if (ioa >= CMD_SEGMENT_QD && ioa < CMD_SEGMENT_QD + CMD_SEGMENT_SIZE) {
                qd_bus = ioa - CMD_SEGMENT_QD;
                qd = sp_value;
            }
        }
        if (system_collapsed()) {
            /* Collapse latch: valid setpoint points refuse with a negative
             * ACT_CON; no setpoint is logged after the grid has collapsed. */
            printf("[server] rejected setpoint bus=%d system collapsed\n",
                qd_bus > 0 ? qd_bus : pd_bus);
            send_negative_con(con, asdu, CS101_COT_ACTIVATION_CON);
            return true;
        }
        IMasterConnection_sendACT_CON(con, asdu, false);
        if (pd_bus > 0) {
            pendingLoadBus = pd_bus;
            pendingLoadPd = pd;
        }
        int bus = qd_bus > 0 ? qd_bus : pd_bus;
        if (qd_bus > 0 && bus == pendingLoadBus) {
            FILE* file = fopen(commandPath, "a");
            if (file) {
                fprintf(file, "{\"action\":\"set_load\",\"bus_id\":%d,\"pd\":%g,\"qd\":%g}\n", bus, pendingLoadPd, qd);
                fclose(file);
            }
            printf("[server] setpoint bus=%d pd=%g qd=%g\n", bus, pendingLoadPd, qd);
            pendingLoadBus = 0;
        }
        IMasterConnection_sendACT_TERM(con, asdu);
        return true;
    }

    /* Unsupported ASDU type: let the stack emit its standard negative
     * response (COT=44 UNKNOWN_TYPE_ID). */
    printf("[server] unhandled ASDU type=%d\n", typeId);
    return false;
}

static bool
interrogationHandler(void* parameter, IMasterConnection con, CS101_ASDU asdu, uint8_t qoi)
{
    (void)asdu;
    printf("[server] interrogation request qoi=%d\n", qoi);

    CS104_Slave slave = (CS104_Slave) parameter;
    CS101_ASDU response = CS101_ASDU_create(CS104_Slave_getAppLayerParameters(slave), false,
                                          CS101_COT_INTERROGATED_BY_STATION, 0, 1, false, false);

    add_all_bus_measurements(response);

    bool sent = IMasterConnection_sendASDUEx(con, response, true);
    CS101_ASDU_destroy(response);

    printf("[server] interrogation response sent=%s\n", sent ? "true" : "false");
    return true;
}

int main(int argc, char** argv)
{
    const char* rateLimitEnv = getenv("CMD_RATE_LIMIT");
    if (rateLimitEnv)
        rateLimitPerWindow = atoi(rateLimitEnv);

    signal(SIGINT, sigint_handler);

    if (argc > 1)
        statePath = argv[1];
    if (argc > 2)
        commandPath = argv[2];
    if (argc > 3) {
        char* token = strtok(argv[3], ",");
        while (token && monitoredBusCount < 256) {
            monitoredBuses[monitoredBusCount++] = atoi(token);
            token = strtok(NULL, ",");
        }
    }

    {
        /* Point table from POWER_LINES ("1-2,1-5,..."), directed pairs
         * exactly as listed in the case branch matrix. Parsed without
         * copying: strtok would be simpler but modifies the string, and
         * getenv() results must not be modified. */
        const char* cursor = getenv("POWER_LINES");
        if (cursor) {
            while (*cursor && branchCount < 512) {
                int from = 0;
                int to = 0;
                int consumed = 0;
                if (sscanf(cursor, "%d-%d%n", &from, &to, &consumed) != 2)
                    break;
                branchFrom[branchCount] = from;
                branchTo[branchCount] = to;
                branchCount++;
                cursor += consumed;
                if (*cursor == ',')
                    cursor++;
                else if (*cursor != '\0')
                    break; /* malformed tail: keep the entries parsed so far */
            }
        }
        printf("[server] point table: %d lines\n", branchCount);
    }

    CS104_Slave slave = CS104_Slave_create(10, 10);
    CS104_Slave_setLocalAddress(slave, "0.0.0.0");
    CS104_Slave_setServerMode(slave, CS104_MODE_SINGLE_REDUNDANCY_GROUP);
    CS104_Slave_setConnectionEventHandler(slave, connectionEventHandler, NULL);
    CS104_Slave_setASDUHandler(slave, asduHandler, NULL);
    CS104_Slave_setInterrogationHandler(slave, interrogationHandler, (void*) slave);
    CS104_Slave_start(slave);

    if (!CS104_Slave_isRunning(slave)) {
        printf("[server] failed to start\n");
        return 1;
    }

    printf("[server] listening on 0.0.0.0:2404\n");

    while (running) {
        CS101_ASDU asdu = CS101_ASDU_create(CS104_Slave_getAppLayerParameters(slave), false, CS101_COT_PERIODIC, 0, 1, false, false);
         add_all_bus_measurements(asdu);
        CS104_Slave_enqueueASDU(slave, asdu);
        CS101_ASDU_destroy(asdu);
         printf("[server] periodic per-bus telemetry buses=%d lastCommandIoa=%d lastCommandValue=%d\n",
             monitoredBusCount, lastCommandIoa, lastCommandValue);
        Thread_sleep(2000);
    }

    CS104_Slave_stop(slave);
    CS104_Slave_destroy(slave);
    return 0;
}
