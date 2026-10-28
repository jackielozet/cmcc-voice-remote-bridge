/*
 * CMCC 语音遥控器 -> USB HID 桥（ESP32-S3 版）
 * ---------------------------------------------------------------------------
 * 链路：  遥控器 --BLE(HOGP)--> ESP32-S3 --USB HID--> Windows
 *
 * 为什么用 S3：
 *   Windows 自己的蓝牙栈不收这个遥控器（3 个 Report Reference 与 Report Map
 *   不符，mshidumdf 严格校验后拒绝初始化，设备管理器稳定报代码 10）。
 *   而对一个通用 BLE 中心来说，0x1812 只是一组普通 GATT 特征，没有那套校验，
 *   所以 ESP32 能像安卓手机一样正常读到按键上报。
 *
 * 三个 USB 身份（TinyUSB 复合设备，一根线全解决）：
 *   1. HID 键盘        —— 映射后的按键输出
 *   2. HID 消费者控制  —— 音量/静音这类多媒体键
 *   3. CDC 串口        —— 给 Windows 设置端 exe 读写映射表
 *
 * 映射表存在 NVS 里，改完拔了 exe 也照常工作，插上电脑就是键盘。
 *
 * 按键标识 id 编码：  id = (源 << 12) | usage
 *   源 1 = 键盘报告(report 0x01)的 usage, 如 0x1004
 *   源 2 = 消费者报告(report 0x03)的 usage, 如 0x20E9（音量+）
 *
 * 输出动作 outType：
 *   1 = 键盘输出,  keys[] 是 HID usage, 0xE0~0xE7 表示修饰键
 *   2 = 消费者输出, keys[0] | keys[1]<<8 组成 16 位 usage
 */
#include <Arduino.h>
#include <sdkconfig.h>          // 必须有：BLESecurity 的 Bluedroid 版接口靠 CONFIG_BLUEDROID_ENABLED 开关
#include <Preferences.h>
#include <BLEDevice.h>
#include <BLEScan.h>
#include <BLEAdvertisedDevice.h>
#include <BLEClient.h>
#include <BLERemoteService.h>
#include <BLERemoteCharacteristic.h>
#include <BLERemoteDescriptor.h>
#include <BLESecurity.h>
#include <esp_log.h>
#include <host/ble_gap.h>
#include <host/ble_store.h>

#include "USB.h"
#include "USBHIDKeyboard.h"
#include "USBHIDConsumerControl.h"
#include "soc/rtc_cntl_reg.h"   // BOOT 命令：软件跳下载模式

// ---------------------------------------------------------------- 设备参数 --
static const char   *TARGET_NAME = "CMCC_Voice_Remote";
static const char   *TARGET_ADDR = "0c:f3:cc:63:10:e3";   // 字符串比较，避开字节序陷阱

static const uint16_t UUID_HID_SERVICE = 0x1812;
static const uint16_t UUID_REPORT      = 0x2A4D;
static const uint16_t UUID_REPORT_REF  = 0x2908;
static const uint16_t UUID_REPORT_MAP  = 0x2A4B;
static const uint16_t UUID_CCCD        = 0x2902;

static const uint32_t SCAN_SECONDS   = 1;      // 短扫：遥控器按键唤醒后只广播很短一段，扫完立刻连
static const uint32_t RESCAN_DELAY   = 2500;   // 扫空后冷却，避免射频长时间满负荷发热
static const uint32_t HEARTBEAT_MS   = 60000;  // 主动保活，防止空闲断链

// ---------------------------------------------------------------- 映射结构 --
#define MAX_BINDS   64
#define MAX_KEYS    4

struct Bind {
  uint16_t id;         // (源<<12)|usage
  uint8_t  outType;    // 1=键盘 2=消费者
  uint8_t  n;          // keys 有效个数
  uint8_t  keys[MAX_KEYS];
};                     // 8 字节

static Bind   g_binds[MAX_BINDS];
static int    g_bindCount = 0;
static bool   g_dirty = false;      // 有未保存的改动

static Preferences g_prefs;
static const char *NVS_NS = "cmcc_map";
static const char *NVS_KEY = "binds";
static const uint8_t MAP_VERSION = 1;

// ---------------------------------------------------------------- 运行开关 --
static bool g_learnMode = false;    // 学习模式：只上报 id，不发送按键
static bool g_rawMode   = false;    // 原始报告打印
static bool g_enabled   = true;     // 总开关（可以暂时禁用输出）
static bool g_audioDump = true;     // 0xFC 音频帧转储开关（AUDIO 命令）
static bool g_scanDump  = false;    // 扫描时打印所有广播设备（SCANDUMP 命令）

// ---------------------------------------------------------------- 板载灯 --
// 微雪板载 GPIO21 RGB 灯（WS2812）：按键 -> 蓝色闪一下；语音数据流 -> 红色常亮
static const int LED_PIN = 21;
static uint8_t  g_ledState     = 0;   // 0=灭 1=蓝 2=红
static uint32_t g_ledBlueUntil = 0;   // 蓝灯闪烁截止时刻
static volatile bool g_voiceActive = false;   // 正在收 0xFC 数据流
static uint32_t      g_lastVoiceMs = 0;

static void setLed(uint8_t s) {
  if (g_ledState == s) return;
  g_ledState = s;
  if      (s == 2) neopixelWrite(LED_PIN, 60, 0, 0);   // 红
  else if (s == 1) neopixelWrite(LED_PIN, 0, 0, 60);   // 蓝
  else             neopixelWrite(LED_PIN, 0, 0, 0);    // 灭
}

static void updateLed() {
  if (g_voiceActive) {            // 语音流优先
    setLed(2);
    g_ledBlueUntil = 0;
    return;
  }
  if (g_ledBlueUntil != 0 && millis() >= g_ledBlueUntil) g_ledBlueUntil = 0;
  setLed(g_ledBlueUntil != 0 ? 1 : 0);
}

// ---------------------------------------------------------------- 音频通道 --
static void handleAudio(const uint8_t *d, size_t len) {
  bool wasActive = g_voiceActive;
  g_lastVoiceMs = millis();
  g_voiceActive = true;
  if (!wasActive) Serial.println("# A START");
  if (!g_audioDump) return;
  Serial.print("A ");
  for (size_t i = 0; i < len; i++) Serial.printf("%02X", d[i]);
  Serial.println();
}

// ---------------------------------------------------------------- USB 设备 --
static USBHIDKeyboard        Keyboard;
static USBHIDConsumerControl Consumer;

// ---------------------------------------------------------------- 按键事件 --
struct KeyEvent {
  uint16_t id;
  bool     down;
};
static QueueHandle_t g_evtQueue = nullptr;

// ---------------------------------------------------------------- BLE 状态 --
struct ReportSlot {
  uint16_t handle;
  uint8_t  reportId;
  uint8_t  reportType;
  BLERemoteCharacteristic *chr;
  uint8_t  last[16];      // 上一次的原始数据，用于算按下/抬起
  uint8_t  lastLen;
};
static ReportSlot g_reports[12];
static int        g_reportCount = 0;

static BLEClient          *g_client = nullptr;
static BLEAdvertisedDevice *g_target = nullptr;
static volatile bool g_connected = false;
static volatile bool g_scanNow   = true;
static uint32_t g_nextScanAt = 0;
static uint32_t g_lastAlive = 0;

// ============================================================== 默认映射表 ==
// 只放“已经确证”的项：方向键、数字、以及从扫描码反推出来的三个特殊键。
// 其余键（OK/返回/主页/菜单/频道/删除/语音…）等设置端学习后再加，
// 不凭空猜 usage，否则会绑错。
static const Bind DEFAULTS[] = {
  // id       outType  n  keys
  {0x104F, 1, 1, {0x4F, 0, 0, 0}},              // 右 -> 右方向
  {0x1050, 1, 1, {0x50, 0, 0, 0}},              // 左 -> 左方向
  {0x1051, 1, 1, {0x51, 0, 0, 0}},              // 下 -> 下方向
  {0x1052, 1, 1, {0x52, 0, 0, 0}},              // 上 -> 上方向
  {0x101E, 1, 1, {0x1E, 0, 0, 0}},              // 1
  {0x101F, 1, 1, {0x1F, 0, 0, 0}},              // 2
  {0x1020, 1, 1, {0x20, 0, 0, 0}},              // 3
  {0x1021, 1, 1, {0x21, 0, 0, 0}},              // 4
  {0x1022, 1, 1, {0x22, 0, 0, 0}},              // 5
  {0x1023, 1, 1, {0x23, 0, 0, 0}},              // 6
  {0x1024, 1, 1, {0x24, 0, 0, 0}},              // 7
  {0x1025, 1, 1, {0x25, 0, 0, 0}},              // 8
  {0x1026, 1, 1, {0x26, 0, 0, 0}},              // 9
  {0x1027, 1, 1, {0x27, 0, 0, 0}},              // 0
  {0x1028, 1, 1, {0x28, 0, 0, 0}},              // 回车
  {0x1029, 1, 1, {0x29, 0, 0, 0}},              // Esc
  {0x102A, 1, 1, {0x2A, 0, 0, 0}},              // 退格
  {0x104C, 1, 1, {0x4C, 0, 0, 0}},              // Delete
  {0x104B, 1, 1, {0x4B, 0, 0, 0}},              // PageUp
  {0x104E, 1, 1, {0x4E, 0, 0, 0}},              // PageDown
  {0x10CC, 1, 1, {0x4C, 0, 0, 0}},              // "#" 键 -> Delete
  {0x1066, 1, 2, {0xE3, 0x0F, 0, 0}},           // 关机键 -> Win+L（锁屏）
  {0x103E, 1, 2, {0xE3, 0x0B, 0, 0}},           // 录音键 -> Win+H（语音听写）
  {0x20E9, 2, 2, {0xE9, 0x00, 0, 0}},           // 音量+ -> 消费者 0x00E9
  {0x20EA, 2, 2, {0xEA, 0x00, 0, 0}},           // 音量- -> 消费者 0x00EA
  {0x20E2, 2, 2, {0xE2, 0x00, 0, 0}},           // 静音   -> 消费者 0x00E2
  {0x20EB, 1, 2, {0xE0, 0x06, 0, 0}},           // 设置键 -> Ctrl+C
  {0x20F0, 1, 2, {0xE0, 0x19, 0, 0}},           // M 键   -> Ctrl+V
};
static const int DEFAULT_COUNT = sizeof(DEFAULTS) / sizeof(DEFAULTS[0]);

// ============================================================== 映射表操作 ==
static int findBind(uint16_t id) {
  for (int i = 0; i < g_bindCount; i++) {
    if (g_binds[i].id == id) return i;
  }
  return -1;
}

static const char *idSource(uint16_t id) {
  switch (id >> 12) {
    case 1: return "键盘";
    case 2: return "消费者";
    default: return "?";
  }
}

// 写一条绑定进内存表：已有就改，没有就追加
static bool upsertBind(uint16_t id, uint8_t outType, uint8_t n, const uint8_t *keys) {
  Bind nb;
  memset(&nb, 0, sizeof(nb));
  nb.id      = id;
  nb.outType = outType;
  nb.n       = n;
  for (int i = 0; i < n && i < MAX_KEYS; i++) nb.keys[i] = keys[i];

  int idx = findBind(id);
  if (idx < 0) {
    if (g_bindCount >= MAX_BINDS) return false;
    idx = g_bindCount++;
  }
  g_binds[idx] = nb;
  g_dirty = true;
  return true;
}

static void loadDefaults() {
  g_bindCount = 0;
  for (int i = 0; i < DEFAULT_COUNT && g_bindCount < MAX_BINDS; i++) {
    g_binds[g_bindCount++] = DEFAULTS[i];
  }
  g_dirty = true;
}

static bool loadBinds() {
  size_t want = sizeof(uint32_t) + MAX_BINDS * sizeof(Bind);
  if (g_prefs.getBytesLength(NVS_KEY) != want) return false;

  uint8_t *buf = (uint8_t *)malloc(want);
  if (buf == nullptr) return false;
  size_t got = g_prefs.getBytes(NVS_KEY, buf, want);
  if (got != want) { free(buf); return false; }

  uint32_t ver = *(uint32_t *)buf;
  if (ver != MAP_VERSION) { free(buf); return false; }

  memcpy(g_binds, buf + sizeof(uint32_t), MAX_BINDS * sizeof(Bind));
  free(buf);

  // 统计有效条数（id 为 0 表示空位）
  g_bindCount = 0;
  for (int i = 0; i < MAX_BINDS; i++) {
    if (g_binds[i].id != 0) g_bindCount = i + 1;
  }
  g_dirty = false;
  return true;
}

static bool saveBinds() {
  size_t want = sizeof(uint32_t) + MAX_BINDS * sizeof(Bind);
  uint8_t *buf = (uint8_t *)malloc(want);
  if (buf == nullptr) return false;
  *(uint32_t *)buf = MAP_VERSION;
  memset(buf + sizeof(uint32_t), 0, MAX_BINDS * sizeof(Bind));
  memcpy(buf + sizeof(uint32_t), g_binds, g_bindCount * sizeof(Bind));
  bool ok = (g_prefs.putBytes(NVS_KEY, buf, want) == want);
  free(buf);
  if (ok) g_dirty = false;
  return ok;
}

// 一次性迁移：把后来定下来的映射补进已有的闪存表。
// 用独立 NVS 版本号控制，跑过就不再动；之后在配置端改这几个键不会被打回。
static void applyMigrations() {
  static const char *MIG_KEY = "migv";
  if (g_prefs.getUChar(MIG_KEY, 0) >= 2) return;

  static const uint8_t WIN_L[] = {0xE3, 0x0F};   // 左 Win + L
  static const uint8_t WIN_H[] = {0xE3, 0x0B};   // 左 Win + H
  static const uint8_t DEL[]   = {0x4C};         // Delete Forward
  upsertBind(0x1066, 1, 2, WIN_L);               // 关机键 -> Win+L（锁屏）
  upsertBind(0x10CC, 1, 1, DEL);                 // "#"    -> Delete
  upsertBind(0x103E, 1, 2, WIN_H);               // 录音键 -> Win+H（语音听写）
  if (saveBinds()) {
    g_prefs.putUChar(MIG_KEY, 2);
    Serial.println("# 迁移 v2：关机键->Win+L，# -> Delete，录音->Win+H");
  }
}

// ============================================================== 按键执行 ====
static void pressBind(const Bind *b) {
  if (!g_enabled) return;
  if (b->outType == 2) {
    uint16_t usage = (uint16_t)b->keys[0] | ((uint16_t)b->keys[1] << 8);
    Consumer.press(usage);
    return;
  }
  for (int i = 0; i < b->n; i++) {
    if (b->keys[i]) Keyboard.pressRaw(b->keys[i]);
  }
}

static void releaseBind(const Bind *b) {
  if (!g_enabled) return;
  if (b->outType == 2) {
    Consumer.release();
    return;
  }
  for (int i = b->n - 1; i >= 0; i--) {
    if (b->keys[i]) Keyboard.releaseRaw(b->keys[i]);
  }
}

// 把 BLE 任务里拿到的按键塞进队列，真正的 USB 输出在主循环里做
// （TinyUSB 的发送不能在 BLE 回调线程里直接调）
static void queueKey(uint16_t id, bool down) {
  KeyEvent ev = {id, down};
  if (xQueueSend(g_evtQueue, &ev, 0) != pdTRUE) {
    // 队列满说明主循环卡住了，丢弃最旧的一条
    KeyEvent drop;
    xQueueReceive(g_evtQueue, &drop, 0);
    xQueueSend(g_evtQueue, &ev, 0);
  }
}

static void handleKey(uint16_t id, bool down) {
  int idx = findBind(id);
  const Bind *b = (idx >= 0) ? &g_binds[idx] : nullptr;

  static uint32_t lastLog = 0;
  if (millis() - lastLog > 30 || idx < 0) {
    Serial.printf("# KEY %04X %s src=%s usage=0x%03X map=%s\n",
                  id, down ? "DOWN" : "up  ", idSource(id),
                  id & 0x0FFF, b ? "yes" : "NONE");
    lastLog = millis();
  }

  if (down) g_ledBlueUntil = millis() + 90;    // 任意按键 -> 蓝灯闪一下

  if (g_learnMode) {
    if (down) Serial.printf("L %04X\n", id);   // 给设置端学习用
    return;
  }
  if (b == nullptr) return;

  if (down) pressBind(b);
  else      releaseBind(b);
}

// ============================================================== BLE 解码 ====
// 键盘报告：mod(1) + 保留(1) + 6 个键码，可能带 report id 前缀
static void decodeKeyboard(const uint8_t *d, size_t n) {
  const uint8_t *k;
  size_t kn;

  if (n >= 9 && d[0] == 0x01) { k = d + 1; kn = n - 1; }  // 带 report id
  else                        { k = d;     kn = n; }

  if (kn < 8) return;

  // 当前按下的键码集合（跳过修饰位和保留位）
  bool now[256] = {false};
  for (int i = 2; i < 8; i++) {
    if (k[i]) now[k[i]] = true;
  }

  uint16_t h = g_reports[0].handle;  // 仅用于日志
  (void)h;

  // 与上一帧比较，得到按下/抬起
  static bool prev[256] = {false};
  for (int c = 0; c < 256; c++) {
    if (now[c] && !prev[c]) queueKey((uint16_t)(0x1000 | c), true);
    if (!now[c] && prev[c]) queueKey((uint16_t)(0x1000 | c), false);
  }
  // 修饰键本身也可能是键（例如遥控器把 Shift 当普通键用）
  uint8_t mod = k[0];
  static uint8_t prevMod = 0;
  for (int bit = 0; bit < 4; bit++) {
    bool a = mod     & (1 << bit);
    bool b = prevMod & (1 << bit);
    if (a && !b) queueKey((uint16_t)(0x1000 | (0xE0 + bit)), true);
    if (!a && b) queueKey((uint16_t)(0x1000 | (0xE0 + bit)), false);
  }
  prevMod = mod;
  memcpy(prev, now, sizeof(prev));
}

// 消费者报告：16 位 usage，可能带 report id 前缀
static void decodeConsumer(const uint8_t *d, size_t n) {
  const uint8_t *k;
  size_t kn;

  if (n >= 3 && d[0] == 0x03) { k = d + 1; kn = n - 1; }
  else                        { k = d;     kn = n; }

  if (kn < 2) return;
  uint16_t usage = (uint16_t)k[0] | ((uint16_t)k[1] << 8);

  static uint16_t prev = 0;
  if (usage == prev) return;
  if (prev) queueKey((uint16_t)(0x2000 | prev), false);
  if (usage) queueKey((uint16_t)(0x2000 | usage), true);
  prev = usage;
}

static void onNotify(BLERemoteCharacteristic *chr, uint8_t *data, size_t len, bool isNotify) {
  ReportSlot *slot = nullptr;
  for (int i = 0; i < g_reportCount; i++) {
    if (g_reports[i].handle == chr->getHandle()) { slot = &g_reports[i]; break; }
  }
  if (slot == nullptr) return;

  if (g_rawMode) {
    Serial.printf("# RAW h=0x%04X id=0x%02X len=%u | ", chr->getHandle(),
                  slot->reportId, (unsigned)len);
    for (size_t i = 0; i < len && i < 20; i++) Serial.printf("%02X ", data[i]);
    Serial.println();
  }

  if (slot->reportId == 0x01) decodeKeyboard(data, len);
  else if (slot->reportId == 0x03) decodeConsumer(data, len);
  else if (slot->reportId == 0xFC) handleAudio(data, len);   // 语音数据通道
  // 其它 report id（0xF8 等厂商数据）暂不处理，RAW 模式下可以看到
}

// ============================================================== BLE 连接 ====
// 抓断开原因：遥控器到底为什么把我们踢掉
static int gapLog(struct ble_gap_event *ev, void *arg) {
  switch (ev->type) {
    case BLE_GAP_EVENT_CONNECT:
      Serial.printf("# GAP CONNECT status=%d\n", ev->connect.status);
      break;
    case BLE_GAP_EVENT_DISCONNECT:
      Serial.printf("# GAP DISCONNECT reason=0x%02X\n", ev->disconnect.reason);
      break;
    case BLE_GAP_EVENT_ENC_CHANGE:
      Serial.printf("# GAP ENC status=%d\n", ev->enc_change.status);
      break;
    case BLE_GAP_EVENT_SUBSCRIBE:
      Serial.printf("# GAP SUBSCRIBE attr=0x%04X cur=%d\n",
                    ev->subscribe.attr_handle, ev->subscribe.cur_notify);
      break;
    default:
      break;
  }
  return 0;
}
// 遥控器广播里的厂商数据尾巴固定是 "LY RC"（company 0x4649 = 讯飞）。
// 用它做指纹匹配，比 MAC 可靠：遥控器做定向广播时，Bluedroid 会把地址
// 报成 00:00:00:00:00:00，按 MAC 匹配就会永远"扫不到"。
static bool payloadIsRemote(BLEAdvertisedDevice &dev) {
  uint8_t *p = dev.getPayload();
  int n = dev.getPayloadLength();
  if (p == nullptr || n < 5) return false;
  static const uint8_t SIG[5] = {0x4C, 0x59, 0x20, 0x52, 0x43};   // 'L','Y',' ','R','C'
  for (int i = 0; i + 5 <= n; i++) {
    if (memcmp(p + i, SIG, 5) == 0) return true;
  }
  return false;
}

class ScanHandler : public BLEAdvertisedDeviceCallbacks {
 public:
  void onResult(BLEAdvertisedDevice dev) override {
    if (g_scanDump) {
      // advType: 0=ADV_IND 1=ADV_DIRECT_IND(定向广播,只理绑定的主机) 2=SCAN_IND 3=NONCONN_IND 4=SCAN_RSP
      Serial.printf("# ADV %s rssi=%d aType=%d advType=%d conn=%d name='%s'\n",
                    dev.getAddress().toString().c_str(), dev.getRSSI(),
                    dev.getAddressType(), dev.getAdvType(),
                    dev.isConnectable() ? 1 : 0,
                    dev.haveName() ? dev.getName().c_str() : "");
      // 原始负载，用来判断是不是定向广播（负载前 6 字节为目标地址）
      uint8_t *p = dev.getPayload();
      int plen = dev.getPayloadLength();
      if (p != nullptr && plen > 0) {
        Serial.print("#   payload ");
        for (int i = 0; i < plen && i < 31; i++) Serial.printf("%02X", p[i]);
        Serial.println();
      }
    }
    if (g_target != nullptr) return;
    bool nameHit = dev.haveName() && dev.getName() == TARGET_NAME;
    bool macHit  = dev.getAddress().toString().equalsIgnoreCase(TARGET_ADDR);
    bool sigHit  = payloadIsRemote(dev);
    if (nameHit || macHit || sigHit) {
      Serial.printf("# 找到遥控器 %s rssi=%d (mac=%d name=%d 指纹=%d)\n",
                    dev.getAddress().toString().c_str(), dev.getRSSI(),
                    macHit ? 1 : 0, nameHit ? 1 : 0, sigHit ? 1 : 0);
      // 注意：绝对不能在这里调 getScan()->stop()。
      // 这个回调跑在协议栈的 GAP 上下文里，在里面停扫描会把状态机卡住，
      // 之后 esp_ble_gattc_open 要等将近半分钟才发得出去（实测 31 秒才失败）。
      // 正确做法：只记下目标，让扫描自然跑完，回到 loop 再连接。
      g_target = new BLEAdvertisedDevice(dev);
      if (!macHit) {
        // 地址异常（全 0）。BLEAdvertisedDevice 的 setAddress 是私有的，
        // 所以连接时统一用已知 MAC，见 connectTarget()
        Serial.println("# 地址异常，连接时改用已知 MAC");
      }
    }
  }
};

static bool setupHid() {
  BLERemoteService *hid = g_client->getService(BLEUUID(UUID_HID_SERVICE));
  if (hid == nullptr) {
    Serial.println("# !! 没发现 0x1812（蓝牙链路可能没加密成功）");
    return false;
  }

  // 必须用"按 handle 索引"的 map：遥控器 7 个 Report 特征的 UUID 全是 0x2A4D，
  // 而 getCharacteristics() 返回的是按 UUID 索引的 map，会互相覆盖只剩一个，
  // 表现为只能收到键盘报告、音量/OK/返回等消费者键完全收不到。
  std::map<uint16_t, BLERemoteCharacteristic *> *chars = hid->getCharacteristicsByHandle();
  if (chars == nullptr || chars->empty()) {
    Serial.println("# !! 0x1812 下看不到特征（多半是链路未加密）");
    return false;
  }

  g_reportCount = 0;
  for (auto &kv : *chars) {
    BLERemoteCharacteristic *c = kv.second;
    if (!c->getUUID().equals(BLEUUID(UUID_REPORT))) continue;
    if (g_reportCount >= (int)(sizeof(g_reports) / sizeof(g_reports[0]))) break;

    ReportSlot &slot = g_reports[g_reportCount];
    slot.handle     = c->getHandle();
    slot.chr        = c;
    slot.reportId   = 0xFF;
    slot.reportType = 0;
    slot.lastLen    = 0;

    BLERemoteDescriptor *ref = c->getDescriptor(BLEUUID(UUID_REPORT_REF));
    if (ref != nullptr) {
      String v = ref->readValue();
      if (v.length() >= 2) {
        slot.reportId   = (uint8_t)v.c_str()[0];
        slot.reportType = (uint8_t)v.c_str()[1];
      }
    }
    g_reportCount++;
  }

  // 把所有报告特征列一遍：id / 类型 / 能否 notify。语音通道和音量键在不在，一看就知道
  Serial.printf("# 共发现 %d 个 Report 特征\n", g_reportCount);
  for (int i = 0; i < g_reportCount; i++) {
    ReportSlot &s = g_reports[i];
    Serial.printf("#   id=0x%02X type=%d h=0x%04X notify=%d\n", s.reportId, s.reportType,
                  s.handle, (s.chr && s.chr->canNotify()) ? 1 : 0);
  }

  // 只订阅键盘 0x01 和消费者 0x03。语音通道 0xFC 先不订：
  // 实测一订 0xFC，遥控器几秒内就把链路断掉。
  int ok = 0;
  const uint8_t want[] = {0x01, 0x03};
  for (int w = 0; w < 2; w++) {
    for (int i = 0; i < g_reportCount; i++) {
      ReportSlot &slot = g_reports[i];
      if (slot.reportId != want[w]) continue;
      if (slot.chr == nullptr || !slot.chr->canNotify()) continue;
      if (slot.reportType != 1) continue;
      slot.chr->registerForNotify(onNotify, true, true);
      Serial.printf("# 订阅 report 0x%02X (h=0x%04X)\n", slot.reportId, slot.handle);
      ok++;
      delay(80);
      break;
    }
  }
  return ok > 0;
}

static bool connectTarget() {
  if (g_target == nullptr) return false;

  // 扫描必须彻底停下再发起连接，否则控制器忙于扫描，连接请求会超时
  BLEDevice::getScan()->stop();
  delay(100);

  // 每次尝试都用全新的 client：上一轮失败后 gattc 注册状态会残留，
  // 复用同一个 client 会让注册信号量白等很久（实测一次失败拖到 31 秒）
  bool ok = false;
  for (int attempt = 1; attempt <= 2 && !ok; attempt++) {
    if (g_client != nullptr) {
      delete g_client;
      g_client = nullptr;
    }
    g_client = BLEDevice::createClient();
    if (g_client == nullptr) {
      Serial.println("# createClient 失败");
      return false;
    }
    g_client->setMTU(247);

    Serial.printf("# t=%lu 连接尝试 %d/2 -> %s\n", millis(), attempt, TARGET_ADDR);

    uint32_t t0 = millis();
    // 统一用已知 MAC 连接：遥控器做定向广播时 Bluedroid 会把它报成
    // 00:00:00:00:00:00，用 adv 里的地址根本连不上。
    BLEAddress peer = BLEAddress(String(TARGET_ADDR));
    ok = g_client->connect(peer, 0, 5000);
    uint32_t dt = millis() - t0;
    Serial.printf("# t=%lu 连接%s 耗时=%lu ms\n", millis(), ok ? "成功" : "失败", (unsigned long)dt);

    // 失败后立刻重试：遥控器按键唤醒后只广播很短一段时间，趁热连才有机会
    if (!ok) delay(150);
  }
  if (!ok) return false;

  // 注意：这里曾经主动发起过配对（BLESecurity::startSecurity），
  // 结果链路在 5.5 秒后被遥控器断掉。原生是"连上就能用"，
  // 说明遥控器不希望被强制走安全流程，所以这里不做任何配对动作。

  if (!setupHid()) {
    Serial.println("# !! 连上了但读不到可订阅的 report（多半是配对/加密没完成）");
    g_client->disconnect();
    return false;
  }

  // 不再主动改连接参数：这句 updateConnParams 是唯一的"自伤"操作，
  // 参数单位在 NimBLE 下容易理解错，先交给协议栈默认值。
  g_lastAlive = millis();
  return true;
}

static void scanOnce() {
  g_target = nullptr;
  BLEScan *scan = BLEDevice::getScan();
  scan->setAdvertisedDeviceCallbacks(new ScanHandler(), false);
  scan->setActiveScan(false);     // 被动扫描：功耗低，只看广播包，不发 SCAN_REQ
  scan->setInterval(300);         // 300ms 周期里只开 30ms 窗 = 10% 占空比
  scan->setWindow(30);
  scan->start(SCAN_SECONDS, false);
  scan->clearResults();
}

// ============================================================== 串口协议 ====
// 全部十六进制不带 0x，行尾 \n。日志行以 '#' 开头，exe 忽略即可。
static void cmdInfo() {
  Serial.printf("OK INFO ver=%d binds=%d learn=%d enabled=%d connected=%d dirty=%d audio=%d\n",
                MAP_VERSION, g_bindCount, g_learnMode ? 1 : 0,
                g_enabled ? 1 : 0, g_connected ? 1 : 0, g_dirty ? 1 : 0,
                g_audioDump ? 1 : 0);
}

static void cmdList() {
  for (int i = 0; i < g_bindCount; i++) {
    Bind &b = g_binds[i];
    if (b.id == 0) continue;
    Serial.printf("B %04X %u %u", b.id, b.outType, b.n);
    for (int k = 0; k < b.n; k++) Serial.printf(" %02X", b.keys[k]);
    Serial.println();
  }
  Serial.println("OK LIST");
}

static void cmdSet(char *args) {
  // SET <id> <outType> <n> <k1> [k2] [k3] [k4]
  char *p = strtok(args, " ");
  if (p == nullptr) { Serial.println("ERR args"); return; }
  uint16_t id = (uint16_t)strtoul(p, nullptr, 16);

  p = strtok(nullptr, " ");
  if (p == nullptr) { Serial.println("ERR args"); return; }
  uint8_t outType = (uint8_t)strtoul(p, nullptr, 10);

  p = strtok(nullptr, " ");
  if (p == nullptr) { Serial.println("ERR args"); return; }
  uint8_t n = (uint8_t)strtoul(p, nullptr, 10);
  if (n > MAX_KEYS) { Serial.println("ERR n"); return; }

  Bind nb;
  memset(&nb, 0, sizeof(nb));
  nb.id = id;
  nb.outType = outType;
  nb.n = n;
  for (int i = 0; i < n; i++) {
    p = strtok(nullptr, " ");
    if (p == nullptr) { Serial.println("ERR keys"); return; }
    nb.keys[i] = (uint8_t)strtoul(p, nullptr, 16);
  }

  int idx = findBind(id);
  if (idx < 0) {
    if (g_bindCount >= MAX_BINDS) { Serial.println("ERR full"); return; }
    idx = g_bindCount++;
  }
  g_binds[idx] = nb;
  g_dirty = true;
  Serial.printf("OK SET %04X\n", id);
}

static void cmdDel(char *args) {
  if (args == nullptr) { Serial.println("ERR args"); return; }
  uint16_t id = (uint16_t)strtoul(args, nullptr, 16);
  int idx = findBind(id);
  if (idx < 0) { Serial.println("ERR notfound"); return; }
  for (int i = idx; i < g_bindCount - 1; i++) g_binds[i] = g_binds[i + 1];
  g_bindCount--;
  memset(&g_binds[g_bindCount], 0, sizeof(Bind));
  g_dirty = true;
  Serial.printf("OK DEL %04X\n", id);
}

static void handleCommand(char *line) {
  // 去掉首尾空白
  while (*line == ' ') line++;
  size_t len = strlen(line);
  while (len > 0 && (line[len - 1] == '\r' || line[len - 1] == ' ')) line[--len] = 0;
  if (len == 0) return;

  char *cmd = line;
  char *args = strchr(line, ' ');
  if (args != nullptr) { *args = 0; args++; }

  if (!strcmp(cmd, "INFO"))   { cmdInfo(); return; }
  if (!strcmp(cmd, "LIST"))   { cmdList(); return; }
  if (!strcmp(cmd, "REPORTS")) {
    Serial.printf("OK REPORTS count=%d\n", g_reportCount);
    for (int i = 0; i < g_reportCount; i++) {
      ReportSlot &s = g_reports[i];
      Serial.printf("R %d id=0x%02X type=%d h=0x%04X notify=%d sub=%d\n", i,
                    s.reportId, s.reportType, s.handle,
                    (s.chr && s.chr->canNotify()) ? 1 : 0, s.lastLen >= 0 ? 1 : 1);
    }
    return;
  }
  if (!strcmp(cmd, "SET"))    { cmdSet(args); return; }
  if (!strcmp(cmd, "DEL"))    { cmdDel(args); return; }
  if (!strcmp(cmd, "LEARN"))  {
    g_learnMode = (args != nullptr && args[0] == '1');
    Serial.printf("OK LEARN %d\n", g_learnMode ? 1 : 0);
    return;
  }
  if (!strcmp(cmd, "RAW"))    {
    g_rawMode = (args != nullptr && args[0] == '1');
    Serial.printf("OK RAW %d\n", g_rawMode ? 1 : 0);
    return;
  }
  if (!strcmp(cmd, "AUDIO"))  {
    g_audioDump = (args != nullptr && args[0] == '1');
    Serial.printf("OK AUDIO %d\n", g_audioDump ? 1 : 0);
    return;
  }
  if (!strcmp(cmd, "SCANDUMP")) {
    g_scanDump = (args != nullptr && args[0] == '1');
    Serial.printf("OK SCANDUMP %d\n", g_scanDump ? 1 : 0);
    return;
  }
  // 软件复位进下载模式：之后用 esptool --before no-reset 直接烧，不必按 BOOT
  if (!strcmp(cmd, "BOOT")) {
    Serial.println("OK BOOT");
    Serial.flush();
    delay(300);
    REG_WRITE(RTC_CNTL_OPTION1_REG, RTC_CNTL_FORCE_DOWNLOAD_BOOT);
    esp_restart();
    return;
  }
  if (!strcmp(cmd, "ENABLE")) {
    g_enabled = (args != nullptr && args[0] == '1');
    if (!g_enabled) Keyboard.releaseAll();
    Serial.printf("OK ENABLE %d\n", g_enabled ? 1 : 0);
    return;
  }
  if (!strcmp(cmd, "SAVE"))   { Serial.println(saveBinds() ? "OK SAVE" : "ERR save"); return; }
  if (!strcmp(cmd, "RESET"))  { loadDefaults(); saveBinds(); Serial.println("OK RESET"); return; }
  if (!strcmp(cmd, "DEFAULTS")) { loadDefaults(); Serial.println("OK DEFAULTS"); return; }
  if (!strcmp(cmd, "CLEAR"))  { g_bindCount = 0; memset(g_binds, 0, sizeof(g_binds)); g_dirty = true; Serial.println("OK CLEAR"); return; }
  if (!strcmp(cmd, "REBOOT")) { Serial.println("OK REBOOT"); delay(200); ESP.restart(); return; }

  Serial.println("ERR unknown");
}

static void pollSerial() {
  static char buf[160];
  static size_t pos = 0;

  while (Serial.available()) {
    int c = Serial.read();
    if (c < 0) break;
    if (c == '\n') {
      buf[pos] = 0;
      handleCommand(buf);
      pos = 0;
    } else if (c != '\r') {
      if (pos < sizeof(buf) - 1) buf[pos++] = (char)c;
      else pos = 0;   // 超长丢弃
    }
  }
}

// ============================================================== setup/loop ==
void setup() {
  Serial.begin(115200);
  delay(600);

  // ---- USB 复合设备：先串口，再键盘 + 消费者控制 ----
  Keyboard.begin();
  Consumer.begin();
  USB.begin();
  delay(400);

  Serial.println();
  Serial.println("# ===== CMCC 遥控器 USB 桥 v2（+语音通道/板载灯） =====");

  // ---- 映射表 ----
  g_prefs.begin(NVS_NS, false);
  if (loadBinds()) {
    Serial.printf("# 映射表已从闪存载入，共 %d 条\n", g_bindCount);
  } else {
    loadDefaults();
    saveBinds();
    Serial.printf("# 首次运行，已写入默认映射 %d 条\n", g_bindCount);
  }
  applyMigrations();

  g_evtQueue = xQueueCreate(32, sizeof(KeyEvent));

  // ---- 蓝牙 ----
  // 打开协议栈原始日志：连接失败时能看到 HCI 层的 status（0x3E 超时 / 0x05 认证失败…）
  esp_log_level_set("*", ESP_LOG_WARN);
  esp_log_level_set("BT_HCI", ESP_LOG_DEBUG);
  esp_log_level_set("BT_BTM", ESP_LOG_DEBUG);
  esp_log_level_set("BT_GATTC", ESP_LOG_DEBUG);
  esp_log_level_set("BT_SMP", ESP_LOG_DEBUG);

  BLEDevice::init("CMCC-Bridge");
  BLEDevice::setCustomGapHandler(gapLog);   // 断开原因

  // NimBLE 默认把配对密钥存进 NVS（CONFIG_BT_NIMBLE_NVS_PERSIST=y）。
  // 之前几轮实验留下的密钥已经不匹配了，不清掉的话订阅 0x03 时
  // 加密失败（GAP ENC status=0x208），遥控器直接把链路掐掉。
  int clr = ble_store_clear();
  Serial.printf("# 清空配对密钥 rc=%d\n", clr);

  BLEDevice::setMTU(247);
  BLESecurity::setAuthenticationMode(true, false, true);   // bond / 无MITM / SC
  BLESecurity::setCapability(ESP_IO_CAP_NONE);
  BLESecurity::setKeySize(16);

  Serial.println("# 就绪。按一下遥控器任意键唤醒它。");
  g_scanNow = true;

  // 连接用的是阻塞式 BLEClient::connect()，一次最长要等 5 秒（栈内部甚至能拖到 30 秒），
  // 远超 Arduino 循环任务看门狗 5 秒的阈值，不关掉会周期性重启。
  disableLoopWDT();
}

void loop() {
  // 1) 串口命令
  pollSerial();

  // 2) 板载灯：语音流 -> 红灯常亮；按键 -> 蓝灯闪；空闲 -> 灭
  if (g_voiceActive && millis() - g_lastVoiceMs > 300) {
    g_voiceActive = false;                     // 300ms 没有新帧 = 语音结束
    Serial.println("# A END");
  }
  updateLed();

  // 3) 队列里的按键 -> USB 输出
  KeyEvent ev;
  while (xQueueReceive(g_evtQueue, &ev, 0) == pdTRUE) {
    handleKey(ev.id, ev.down);
  }

  // 4) 蓝牙状态机
  if (g_connected) {
    if (g_client == nullptr || !g_client->isConnected()) {
      Serial.println("# 断链，重新扫描");
      g_connected = false;
      g_reportCount = 0;
      g_voiceActive = false;
      g_scanNow = true;
      Keyboard.releaseAll();
    } else {
      // 心跳：长时间没交互时主动读一次，避免链路被判定空闲
      if (millis() - g_lastAlive > HEARTBEAT_MS) {
        g_lastAlive = millis();
      }
    }
  }

  if (g_scanNow && millis() >= g_nextScanAt) {
    g_scanNow = false;
    scanOnce();
    Serial.printf("# t=%lu 扫描结束 target=%s\n", millis(), g_target != nullptr ? "有" : "无");
    if (g_target == nullptr) {
      // 扫空就冷却一段时间，别让射频一直满负荷跑（板子会烫）
      g_nextScanAt = millis() + RESCAN_DELAY;
      // 扫不到就静默重试，别刷屏
      static uint32_t lastMsg = 0;
      if (millis() - lastMsg > 15000) {
        lastMsg = millis();
        Serial.println("# 还没扫到遥控器（先按一下遥控器按键唤醒）");
      }
      g_scanNow = true;
    } else {
      g_connected = connectTarget();
      if (!g_connected) {
        g_scanNow = true;
      } else {
        Serial.println("# --- 开始接收按键 ---");
      }
    }
  }

  delay(2);
}
