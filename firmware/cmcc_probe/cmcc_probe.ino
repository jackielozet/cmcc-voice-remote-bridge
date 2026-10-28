/*
 * CMCC_Voice_Remote (0C:F3:CC:63:10:E3) BLE probe
 * Target: ESP32-C3, arduino-esp32 3.3.x
 *
 * Purpose: answer four questions before building anything on top.
 *   Q1  Can a plain BLE central see the 0x1812 HOGP service on this remote?
 *       (Windows cannot, but that is bthleenum.sys filtering, not the device.)
 *   Q2  Can we complete pairing / reach an encrypted link?
 *   Q3  Can we write the Report characteristics' CCCD?
 *   Q4  Do button presses actually arrive as notifications, and what bytes?
 *
 * Nothing here talks USB. It only logs to Serial.
 */

#include <Arduino.h>
#include <BLEDevice.h>
#include <BLEScan.h>
#include <BLEAdvertisedDevice.h>
#include <BLEClient.h>
#include <BLERemoteService.h>
#include <BLERemoteCharacteristic.h>
#include <BLERemoteDescriptor.h>
#include <BLESecurity.h>

// ---------------------------------------------------------------- config ---
static const char *TARGET_NAME = "CMCC_Voice_Remote";
// 0C:F3:CC:63:10:E3
static const uint8_t TARGET_MAC[6] = {0x0C, 0xF3, 0xCC, 0x63, 0x10, 0xE3};

static const uint16_t UUID_HID_SERVICE = 0x1812;
static const uint16_t UUID_REPORT = 0x2A4D;
static const uint16_t UUID_REPORT_REF = 0x2908;
static const uint16_t UUID_REPORT_MAP = 0x2A4B;
static const uint16_t UUID_HID_INFO = 0x2A4A;

static const uint32_t SCAN_SECONDS = 6;
static const uint32_t RESCAN_DELAY_MS = 3000;

// ----------------------------------------------------------------- state ---
struct ReportSlot {
  uint16_t handle;
  uint8_t reportId;
  uint8_t reportType;   // 1=Input 2=Output 3=Feature
  bool subscribed;
  BLERemoteCharacteristic *chr;
};

static ReportSlot g_reports[12];
static int g_reportCount = 0;

static BLEClient *g_client = nullptr;
static BLEAdvertisedDevice *g_target = nullptr;
static volatile bool g_connected = false;
static volatile bool g_scanRequested = false;

// --------------------------------------------------------------- helpers ---
static const char *typeName(uint8_t t) {
  switch (t) {
    case 1: return "IN";
    case 2: return "OUT";
    case 3: return "FEAT";
    default: return "?";
  }
}

static void hexLine(const uint8_t *d, size_t n) {
  for (size_t i = 0; i < n; i++) {
    Serial.printf("%02X ", d[i]);
  }
}

static ReportSlot *slotForHandle(uint16_t h) {
  for (int i = 0; i < g_reportCount; i++) {
    if (g_reports[i].handle == h) return &g_reports[i];
  }
  return nullptr;
}

// ---------------------------------------------------------- notify sink ---
static void onNotify(BLERemoteCharacteristic *chr, uint8_t *data, size_t len, bool isNotify) {
  ReportSlot *slot = slotForHandle(chr->getHandle());
  uint8_t id = slot ? slot->reportId : 0;
  const char *tag = "?";

  if (slot) {
    if (id == 0x01 && len >= 8) {
      // keyboard: mod, reserved, then 6 usage slots
      if (data[0] || data[2] || data[3] || data[4] || data[5] || data[6] || data[7]) {
        tag = "KEY";
      } else {
        return;  // all-zero release frame, keep the log readable
      }
    } else if (id == 0x03 && len >= 2) {
      uint16_t usage = (uint16_t)data[0] | ((uint16_t)data[1] << 8);
      if (usage) {
        tag = "CONS";
      } else {
        return;  // consumer release
      }
    } else if (len > 0 && len <= 4) {
      bool allZero = true;
      for (size_t i = 0; i < len; i++) {
        if (data[i]) { allZero = false; break; }
      }
      if (allZero) return;
      tag = "DATA";
    } else {
      tag = "DATA";
    }
  }

  Serial.printf("[%8lu] %-4s h=0x%04X rpt=0x%02X len=%2u | ",
                (unsigned long)millis(), tag, chr->getHandle(), id, (unsigned)len);
  hexLine(data, len);
  if (slot && id == 0x01 && len >= 8) {
    Serial.printf("| mod=0x%02X keys=", data[0]);
    for (int i = 2; i < 8 && i < (int)len; i++) {
      if (data[i]) Serial.printf("0x%02X ", data[i]);
    }
  } else if (slot && id == 0x03 && len >= 2) {
    Serial.printf("| usage=0x%04X", (unsigned)((uint16_t)data[0] | ((uint16_t)data[1] << 8)));
  }
  Serial.println();
}

// ------------------------------------------------------------------ scan ---
class ScanHandler : public BLEAdvertisedDeviceCallbacks {
 public:
  void onResult(BLEAdvertisedDevice dev) override {
    if (g_target != nullptr) return;

    bool nameHit = dev.haveName() && dev.getName() == TARGET_NAME;
    bool macHit = (memcmp(dev.getAddress().getNative(), TARGET_MAC, 6) == 0);

    if (nameHit || macHit) {
      Serial.printf("  >> MATCH %s  rssi=%d\n",
                    dev.getAddress().toString().c_str(), dev.getRSSI());
      g_target = new BLEAdvertisedDevice(dev);
      BLEDevice::getScan()->stop();
    } else if (dev.haveName()) {
      // useful when the remote ran out of battery or reverted to another name
      Serial.printf("     seen: %-24s %s rssi=%d\n",
                    dev.getName().c_str(), dev.getAddress().toString().c_str(), dev.getRSSI());
    }
  }
};

// ------------------------------------------------------------ discovery ---
static bool dumpServices() {
  std::map<std::string, BLERemoteService *> *services = g_client->getServices();
  if (services == nullptr || services->empty()) {
    Serial.println("  !! no services discovered at all");
    return false;
  }

  Serial.printf("  services: %u\n", (unsigned)services->size());
  bool sawHid = false;

  for (auto &kv : *services) {
    BLERemoteService *svc = kv.second;
    BLEUUID uuid = svc->getUUID();
    bool isHid = uuid.equals(BLEUUID((uint16_t)UUID_HID_SERVICE));
    Serial.printf("  [SVC] %s%s\n", uuid.toString().c_str(), isHid ? "   <== HID" : "");
    if (isHid) sawHid = true;

    std::map<std::string, BLERemoteCharacteristic *> *chars = svc->getCharacteristics();
    if (chars == nullptr || chars->empty()) {
      Serial.println("        (no characteristics — permission or discovery failure)");
      continue;
    }
    for (auto &ckv : *chars) {
      BLERemoteCharacteristic *c = ckv.second;
      BLEUUID cu = c->getUUID();
      Serial.printf("        chr h=0x%04X %s  r=%d w=%d wn=%d n=%d\n",
                    c->getHandle(), cu.toString().c_str(),
                    c->canRead() ? 1 : 0, c->canWrite() ? 1 : 0,
                    c->canWriteNoResponse() ? 1 : 0, c->canNotify() ? 1 : 0);
    }
  }
  return sawHid;
}

static void inspectHidService() {
  BLERemoteService *hid = g_client->getService(BLEUUID(UUID_HID_SERVICE));
  if (hid == nullptr) {
    Serial.println("  !! getService(0x1812) returned null");
    return;
  }

  std::map<std::string, BLERemoteCharacteristic *> *chars = hid->getCharacteristics();
  if (chars == nullptr || chars->empty()) {
    Serial.println("  !! 0x1812 has no characteristics visible to this central");
    return;
  }

  // Report Map — proves we can read protected HID data at all
  BLERemoteCharacteristic *mapChr = hid->getCharacteristic(BLEUUID(UUID_REPORT_MAP));
  if (mapChr != nullptr && mapChr->canRead()) {
    String map = mapChr->readValue();
    Serial.printf("  Report Map: %u bytes\n    ", (unsigned)map.length());
    const uint8_t *p = (const uint8_t *)map.c_str();
    for (size_t i = 0; i < map.length(); i++) {
      Serial.printf("%02X ", p[i]);
      if ((i + 1) % 28 == 0) Serial.print("\n    ");
    }
    Serial.println();
  }

  // Report characteristics + their Report Reference descriptors
  g_reportCount = 0;
  for (auto &kv : *chars) {
    BLERemoteCharacteristic *c = kv.second;
    BLEUUID cu = c->getUUID();
    if (!cu.equals(BLEUUID((uint16_t)UUID_REPORT))) continue;
    if (g_reportCount >= (int)(sizeof(g_reports) / sizeof(g_reports[0]))) break;

    ReportSlot &slot = g_reports[g_reportCount];
    slot.handle = c->getHandle();
    slot.chr = c;
    slot.reportId = 0xFF;
    slot.reportType = 0;
    slot.subscribed = false;

    BLERemoteDescriptor *ref = c->getDescriptor(BLEUUID(UUID_REPORT_REF));
    if (ref != nullptr) {
      String v = ref->readValue();
      if (v.length() >= 2) {
        slot.reportId = (uint8_t)v.c_str()[0];
        slot.reportType = (uint8_t)v.c_str()[1];
      }
    }
    Serial.printf("  REPORT h=0x%04X id=0x%02X type=%-4s notify=%d\n",
                  slot.handle, slot.reportId, typeName(slot.reportType),
                  c->canNotify() ? 1 : 0);
    g_reportCount++;
  }
  Serial.printf("  report characteristics: %d\n", g_reportCount);
}

static void subscribeAll() {
  int ok = 0, fail = 0;
  for (int i = 0; i < g_reportCount; i++) {
    ReportSlot &slot = g_reports[i];
    if (slot.chr == nullptr || !slot.chr->canNotify()) continue;

    // registerForNotify writes the CCCD itself; a protected HID report needs
    // an encrypted link first, so a failure here is meaningful, not noise.
    slot.chr->registerForNotify(onNotify, true, true);
    slot.subscribed = true;
    ok++;

    // read back the CCCD to confirm the write landed
    BLERemoteDescriptor *cccd = slot.chr->getDescriptor(BLEUUID((uint16_t)0x2902));
    if (cccd != nullptr) {
      String v = cccd->readValue();
      if (v.length() >= 2) {
        uint16_t bits = (uint8_t)v.c_str()[0] | ((uint16_t)(uint8_t)v.c_str()[1] << 8);
        Serial.printf("    h=0x%04X CCCD=0x%04X %s\n", slot.handle, bits,
                      (bits & 0x0001) ? "notify ON" : "NOTIFY OFF");
      }
    }
  }
  Serial.printf("  subscribed: %d ok, %d failed\n", ok, fail);
}

// ------------------------------------------------------------- connect ---
static bool connectTarget() {
  if (g_target == nullptr) return false;

  if (g_client == nullptr) {
    g_client = BLEDevice::createClient();
    g_client->setMTU(247);
  }

  Serial.printf("connecting to %s ...\n", g_target->getAddress().toString().c_str());
  if (!g_client->connectTimeout(g_target, 10000)) {
    Serial.println("  !! connect() failed");
    return false;
  }
  Serial.println("  connected");

  // HOGP wants an encrypted link for Report access. Just Works + bonding
  // first; if CCCD writes fail we come back and try MITM.
  bool dumped = dumpServices();
  if (!dumped) {
    Serial.println("  !! HID service not present in discovery");
  }

  inspectHidService();
  subscribeAll();

  // keep the link alive: the remote drops idle connections by design
  g_client->updateConnParams(12, 24, 0, 400);

  return true;
}

static void scanOnce() {
  Serial.println("scanning ...");
  BLEScan *scan = BLEDevice::getScan();
  scan->setAdvertisedDeviceCallbacks(new ScanHandler(), false);
  scan->setActiveScan(true);
  scan->setInterval(100);
  scan->setWindow(99);
  scan->start(SCAN_SECONDS, false);
  scan->clearResults();
}

// ---------------------------------------------------------------- setup ---
void setup() {
  Serial.begin(115200);
  delay(1200);
  Serial.println();
  Serial.println("=== CMCC Voice Remote BLE probe ===");
  Serial.printf("target %s / 0C:F3:CC:63:10:E3\n", TARGET_NAME);

  BLEDevice::init("cmcc-probe");
  BLEDevice::setMTU(247);

  // bonding on, MITM off for the first attempt, secure connections on
  BLESecurity::setAuthenticationMode(true, false, true);
  BLESecurity::setCapability(ESP_IO_CAP_NONE);
  BLESecurity::setKeySize(16);

  Serial.println("ready. press a button on the remote to wake it.");
  g_scanRequested = true;
}

// ----------------------------------------------------------------- loop ---
void loop() {
  if (g_connected) {
    if (g_client != nullptr && !g_client->isConnected()) {
      Serial.println("disconnected");
      g_connected = false;
      g_reportCount = 0;
      g_scanRequested = true;
    }
    delay(200);
    return;
  }

  if (g_scanRequested) {
    g_scanRequested = false;
    g_target = nullptr;
    scanOnce();

    if (g_target == nullptr) {
      Serial.println("  no match; retrying");
      delay(RESCAN_DELAY_MS);
      g_scanRequested = true;
      return;
    }

    if (connectTarget()) {
      g_connected = true;
      Serial.println("--- listening. press buttons now ---");
    } else {
      Serial.println("  connect failed; will rescan");
      delay(RESCAN_DELAY_MS);
      g_scanRequested = true;
    }
  }
}
