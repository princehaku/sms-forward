PROJECT = "SMSCENTER"
VERSION = "2.2.7"

require "log"
LOG_LEVEL = log.LOGLEVEL_INFO
require "sys"
require "net"
require "netLed"

-- GK21.5PTM rev0.3 routes the marked network indicator to Air724UG
-- physical pin 53, SPI1_DIN/GPIO12.
netLed.setup(true, pio.P0_12)

-- Free the USB port from RNDIS mode so it can be used for logs and downloading.
ril.request("AT+RNDISCALL=0,1")

require "sms_center"

sys.init(0, 0)
sys.run()
