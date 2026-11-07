CC = gcc
LIB_DIR = ../lib60870-2.4.1/lib60870-C
CFLAGS = -std=gnu99 -Wall -Wextra -Werror -I$(LIB_DIR)/config -I$(LIB_DIR)/src/inc/api -I$(LIB_DIR)/src/inc/internal -I$(LIB_DIR)/src/hal/inc -I$(LIB_DIR)/src/common/inc -pthread
LDFLAGS = -L$(LIB_DIR)/build -llib60870 -lpthread

all: scada_server scada_client

scada_server: scada_server.c
	$(CC) $(CFLAGS) -o $@ $< $(LDFLAGS)

scada_client: scada_client.c
	$(CC) $(CFLAGS) -o $@ $< $(LDFLAGS)

clean:
	rm -f scada_server scada_client
