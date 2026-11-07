#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include "cs104_connection.h"
#include "cs101_information_objects.h"
#include "hal_thread.h"

static void connectionHandler(void* parameter, CS104_Connection connection, CS104_ConnectionEvent event)
{
    (void)parameter;
    (void)connection;
    if (event == CS104_CONNECTION_OPENED)
        printf("[client] connection opened\n");
    else if (event == CS104_CONNECTION_CLOSED)
        printf("[client] connection closed\n");
}

static bool asduReceivedHandler(void* parameter, int address, CS101_ASDU asdu)
{
    (void)parameter;
    (void)address;

    int typeId = CS101_ASDU_getTypeID(asdu);
    printf("[client] ASDU received type=%d elements=%d\n", typeId, CS101_ASDU_getNumberOfElements(asdu));

    if (typeId == M_ME_NA_1 || typeId == M_ME_NB_1 || typeId == M_ME_TA_1 || typeId == M_ME_TE_1) {
        for (int i = 0; i < CS101_ASDU_getNumberOfElements(asdu); i++) {
            MeasuredValueScaled io = (MeasuredValueScaled) CS101_ASDU_getElement(asdu, i);
            if (io) {
                int ioa = InformationObject_getObjectAddress((InformationObject) io);
                const char* field = "unknown";
                int bus = 0;
                if (ioa >= 1000 && ioa < 2000) {
                    field = "voltage";
                    bus = ioa - 1000;
                } else if (ioa >= 2000 && ioa < 3000) {
                    field = "active_load";
                    bus = ioa - 2000;
                } else if (ioa >= 3000 && ioa < 4000) {
                    field = "reactive_load";
                    bus = ioa - 3000;
                } else if (ioa >= 4000 && ioa < 5000) {
                    field = "generator_output";
                    bus = ioa - 4000;
                }
                const char* qtext;
                int quality = MeasuredValueScaled_getQuality(io);
                if (quality == IEC60870_QUALITY_GOOD)
                    qtext = "GOOD";
                else if (quality == IEC60870_QUALITY_INVALID)
                    qtext = "INVALID";
                else
                    qtext = "OTHER";
                printf("[client] bus=%d field=%s IOA=%d value=%d quality=0x%02x(%s)\n", bus, field, ioa,
                       MeasuredValueScaled_getValue(io), quality, qtext);
                MeasuredValueScaled_destroy(io);
            }
        }
    }

    if (typeId == C_SC_NA_1) {
        SingleCommand sc = (SingleCommand) CS101_ASDU_getElement(asdu, 0);
        if (sc) {
            printf("[client] ACK IOA=%d state=%d\n",
                   InformationObject_getObjectAddress((InformationObject) sc),
                   SingleCommand_getState(sc));
            SingleCommand_destroy(sc);
        }
    }

    return true;
}

static bool send_control(CS104_Connection con, int argc, char** argv)
{
    if (argc < 1)
        return true;

    if (strcmp(argv[0], "trip") == 0 || strcmp(argv[0], "close") == 0) {
        if (argc != 3)
            return false;
        int from_bus = atoi(argv[1]);
        int to_bus = atoi(argv[2]);
        /* IOA layout: segment (100000 trip / 200000 close) + from*128 + to.
         * Base 128 supports bus numbers up to 127 (largest case: IEEE-118). */
        int ioa = (strcmp(argv[0], "trip") == 0 ? 100000 : 200000) + from_bus * 128 + to_bus;
        InformationObject command = (InformationObject) SingleCommand_create(NULL, ioa, true, false, 0);
        bool sent = CS104_Connection_sendProcessCommandEx(con, CS101_COT_ACTIVATION, 1, command);
        InformationObject_destroy(command);
        printf("[client] sent %s_line %d-%d ioa=%d\n", argv[0], from_bus, to_bus, ioa);
        return sent;
    }

    if (strcmp(argv[0], "set_load") == 0) {
        if (argc != 4)
            return false;
        int bus = atoi(argv[1]);
        float pd = (float) atof(argv[2]);
        float qd = (float) atof(argv[3]);
        CS101_AppLayerParameters parameters = CS104_Connection_getAppLayerParameters(con);
        CS101_ASDU pd_asdu = CS101_ASDU_create(parameters, false, CS101_COT_ACTIVATION, 0, 1, false, false);
        InformationObject pd_command = (InformationObject) SetpointCommandShort_create(NULL, 300000 + bus, pd, false, 0);
        CS101_ASDU_addInformationObject(pd_asdu, pd_command);
        bool sent = CS104_Connection_sendASDU(con, pd_asdu);
        InformationObject_destroy(pd_command);
        CS101_ASDU_destroy(pd_asdu);

        CS101_ASDU qd_asdu = CS101_ASDU_create(parameters, false, CS101_COT_ACTIVATION, 0, 1, false, false);
        InformationObject qd_command = (InformationObject) SetpointCommandShort_create(NULL, 400000 + bus, qd, false, 0);
        CS101_ASDU_addInformationObject(qd_asdu, qd_command);
        sent = CS104_Connection_sendASDU(con, qd_asdu) && sent;
        InformationObject_destroy(qd_command);
        CS101_ASDU_destroy(qd_asdu);
        printf("[client] sent set_load bus=%d pd=%g qd=%g\n", bus, pd, qd);
        return sent;
    }

    return false;
}

int main(int argc, char** argv)
{
    const char* host = (argc > 1) ? argv[1] : "127.0.0.1";
    int port = (argc > 2) ? atoi(argv[2]) : 2404;
    int commandIoa = (argc > 3) ? atoi(argv[3]) : 0;
    int commandValue = (argc > 4) ? atoi(argv[4]) : 1;

    printf("[client] connecting to %s:%d\n", host, port);

    CS104_Connection con = CS104_Connection_create(host, port);
    CS104_Connection_setConnectionHandler(con, connectionHandler, NULL);
    CS104_Connection_setASDUReceivedHandler(con, asduReceivedHandler, NULL);

    if (CS104_Connection_connect(con)) {
        printf("[client] connected\n");
        CS104_Connection_sendStartDT(con);
        Thread_sleep(2000);
        CS104_Connection_sendInterrogationCommand(con, CS101_COT_ACTIVATION, 1, IEC60870_QOI_STATION);
        Thread_sleep(3000);

        if (argc > 5 && strcmp(argv[3], "trip") != 0 && strcmp(argv[3], "close") != 0 && strcmp(argv[3], "set_load") != 0) {
            printf("[client] invalid control command\n");
        } else if (argc > 5 && (strcmp(argv[3], "trip") == 0 || strcmp(argv[3], "close") == 0 || strcmp(argv[3], "set_load") == 0)) {
            send_control(con, argc - 3, argv + 3);
        } else if (commandIoa > 0) {
            InformationObject sc = (InformationObject) SingleCommand_create(NULL, commandIoa, commandValue != 0, false, 0);
            printf("[client] sending C_SC_NA_1 command to IOA %d\n", commandIoa);
            CS104_Connection_sendProcessCommandEx(con, CS101_COT_ACTIVATION, 1, sc);
            InformationObject_destroy(sc);
        }

        Thread_sleep((argc > 5) ? atoi(argv[5]) * 1000 : 2000);
    } else {
        printf("[client] connect failed\n");
    }

    CS104_Connection_destroy(con);
    return 0;
}
