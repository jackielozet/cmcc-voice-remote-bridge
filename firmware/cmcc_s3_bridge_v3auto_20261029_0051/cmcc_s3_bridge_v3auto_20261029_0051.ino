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
 *
 * ---------------------------------------------------------------------------
 * v3（2026-10-29）自动识别版，与上一版并存，不覆盖
 * ---------------------------------------------------------------------------
 * 上一版把"某一台遥控器"的身份写死在代码里：广播名、MAC、厂商广播指纹
 * 三条都是常量，换一只遥控器就完全不动。
 *
 * 本版把所有设备身份从代码里搬走，改成运行时判断：
 *   1. 发现：按广播包里的通用 HID 特征打分（Appearance 0x03C0~0x03C4 /
 *      服务 UUID 0x1812 / 可配置的名字前缀、厂商 ID、厂商标记），取分最高者；
 *      连上过一次后自动把地址记进 NVS，下次直接认地址，不再靠猜。
 *   2. 连接：用广播里报的真实地址；只有拿不到地址（全 0）时才要求手工指定。
 *   3. 报告：读 0x2A4B Report Map 描述符，按顶层集合（Keyboard /
 *      Consumer Control）和位宽自动判断每个 report 是键盘、消费者还是数据通道，
 *      不再写死 0x01 / 0x03 / 0xFC。
 *   4. 兜底：描述符读不到或解析不出东西时，退回上一版的规则
 *      （0x01 键盘 / 0x03 消费者），保证老设备行为不变。
 *
 * 新增串口命令：CFG / TARGET / MAPDUMP（见 handleCommand）
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
#include <strings.h>             // strcasecmp
#include <host/ble_gap.h>
#include <host/ble_store.h>

#include "USB.h"
#include "USBHIDKeyboard.h"
#include "USBHIDConsumerControl.h"
#include "soc/rtc_cntl_reg.h"   // BOOT 命令：软件跳下载模式

// ---------------------------------------------------------------- 设备识别 --
// 这里不再出现任何"某一台遥控器"的信息。识别条件要么来自广播包本身，
// 要么来自 NVS 里的配置（CFG / TARGET 命令可改），出厂默认对所有 BLE HID 设备通用。
#define CFG_MAGIC 0x33435243UL        // 'CRC3'
// 配置和映射表共用一个 NVS 命名空间（不同的键），省一次 begin()
static const char *CFG_KEY = "dev";

struct DevCfg {
  uint32_t magic;
  char     pin[18];      // 固定 / 已学习到的地址，空串 = 自动发现
  char     npfx[16];     // 广播名前缀，空 = 不限制
  uint16_t cid;          // 厂商 ID（0xFF 厂商数据前两字节），0 = 不限制
  uint8_t  anyDev;       // 1 = 任何可连接设备都可作候选（最后手段，默认关）
  uint8_t  autoPin;      // 1 = 首次连上后把地址写进 NVS（默认开）
  uint8_t  useApp;       // 1 = 认 Appearance 0x03C0~0x03C4（HID 类，默认开）
  uint8_t  useHidUuid;   // 1 = 认广播里的服务 UUID 0x1812（默认开）
};
static DevCfg g_cfg;
// 注意：这里不能出现任何函数定义。Arduino 会把函数原型插到"第一个函数定义"
// 之前，如果那个定义早于 struct Bind，原型里引用 Bind 就会编译失败。
// cfgDefaults() 放在映射表那一段之后。

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
// report 的角色：由 HID Report Map 描述符推导，不再靠写死 id 判断
#define ROLE_NONE 0     // 认不出来（走老规则）
#define ROLE_KBD  1     // 键盘：修饰键 + 6 键码
#define ROLE_CONS 2     // 消费者控制：16 位 usage
#define ROLE_DATA 3     // 大块数据通道（0xFC 这类），一律不订阅
struct ReportSlot {
  uint16_t handle;
  uint8_t  reportId;
  uint8_t  reportType;
  uint8_t  role;          // 0=未知(走老规则) 1=键盘 2=消费者 3=数据通道(不订阅)
  uint16_t bytes;         // 描述符里算出来的单帧字节数
  BLERemoteCharacteristic *chr;
  uint8_t  last[16];      // 上一次的原始数据，用于算按下/抬起
  uint8_t  lastLen;
};
static ReportSlot g_reports[12];
static int        g_reportCount = 0;

static BLEClient          *g_client = nullptr;
static BLEAdvertisedDevice *g_target = nullptr;
static int  g_targetScore = 0;          // 当前候选的匹配分数
static char g_targetWhy[16] = "";       // 命中的条件，便于排查
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

// ------------------------------------------------------------ 设备识别配置 --
// 存在和映射表同一个 NVS 命名空间的另一个键里，格式变了用 magic 判掉、回默认。
static void cfgDefaults() {
  memset(&g_cfg, 0, sizeof(g_cfg));
  g_cfg.magic      = CFG_MAGIC;
  g_cfg.anyDev     = 0;
  g_cfg.autoPin    = 1;
  g_cfg.useApp     = 1;
  g_cfg.useHidUuid = 1;
}

static void saveCfg() {
  g_cfg.magic = CFG_MAGIC;
  g_prefs.putBytes(CFG_KEY, &g_cfg, sizeof(g_cfg));
}

static void loadCfg() {
  cfgDefaults();
  if (g_prefs.getBytesLength(CFG_KEY) != sizeof(g_cfg)) return;
  DevCfg tmp;
  if (g_prefs.getBytes(CFG_KEY, &tmp, sizeof(tmp)) != sizeof(tmp)) return;
  if (tmp.magic != CFG_MAGIC) return;
  g_cfg = tmp;
  g_cfg.pin [sizeof(g_cfg.pin)  - 1] = 0;   // 防越界字符串
  g_cfg.npfx[sizeof(g_cfg.npfx) - 1] = 0;
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

  // 有角色就按角色分发；没有角色（描述符没读到）时退回按 id 判断，与上一版一致。
  if (slot->role == ROLE_KBD)        decodeKeyboard(data, len);
  else if (slot->role == ROLE_CONS)  decodeConsumer(data, len);
  else if (slot->role == ROLE_DATA)  handleAudio(data, len);   // 语音数据通道
  else if (slot->reportId == 0x01)   decodeKeyboard(data, len);
  else if (slot->reportId == 0x03)   decodeConsumer(data, len);
  else if (slot->reportId == 0xFC)   handleAudio(data, len);
  // 其余（鼠标、厂商自定义）不处理，RAW 模式下可以看到原始帧
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
// ---- 广播包（AD 结构）解析：不假设任何字段顺序或有无，逐条遍历 ----
// 结构：len(1) type(1) data(len-1)
static const uint8_t *adFind(const uint8_t *p, int n, uint8_t type, int *outLen) {
  int i = 0;
  while (i + 1 < n) {
    int len = p[i];
    if (len <= 0) break;
    if (i + 1 + len > n) break;
    if (p[i + 1] == type) {
      if (outLen) *outLen = len - 1;
      return p + i + 2;
    }
    i += 1 + len;
  }
  return nullptr;
}

// 0x19 Appearance。0x03C0~0x03C4 = HID Generic / 键盘 / 鼠标 / 摇杆 / 游戏手柄，
// 这是蓝牙 SIG 定义的通用值，任何 BLE HID 输入设备都可能带，不针对某一只遥控器。
static uint16_t adAppearance(const uint8_t *p, int n, bool *has) {
  int len = 0;
  const uint8_t *v = adFind(p, n, 0x19, &len);
  if (v == nullptr || len < 2) { if (has) *has = false; return 0; }
  if (has) *has = true;
  return (uint16_t)v[0] | ((uint16_t)v[1] << 8);
}

// 0x02 / 0x03 16 位服务 UUID 列表，以及 0x16 服务数据里的 UUID
static bool adHasUuid16(const uint8_t *p, int n, uint16_t uuid) {
  int len = 0;
  const uint8_t *v;
  if ((v = adFind(p, n, 0x03, &len)) || (v = adFind(p, n, 0x02, &len))) {
    for (int i = 0; i + 1 < len; i += 2) {
      if (((uint16_t)v[i] | ((uint16_t)v[i + 1] << 8)) == uuid) return true;
    }
  }
  if ((v = adFind(p, n, 0x16, &len)) && len >= 2) {
    if (((uint16_t)v[0] | ((uint16_t)v[1] << 8)) == uuid) return true;
  }
  return false;
}

// 0x08 / 0x09 广播名（可空）。有些设备名字只在扫描响应里，这里只看主广播包。
static int adName(const uint8_t *p, int n, char *out, int cap) {
  int len = 0;
  const uint8_t *v = adFind(p, n, 0x09, &len);
  if (v == nullptr) v = adFind(p, n, 0x08, &len);
  if (v == nullptr || len <= 0) { out[0] = 0; return 0; }
  int c = len < cap - 1 ? len : cap - 1;
  memcpy(out, v, c);
  out[c] = 0;
  return c;
}

// 0xFF 厂商自定义数据：前 2 字节是 Bluetooth SIG 分配的 company id（小端）
static bool adVendorCid(const uint8_t *p, int n, uint16_t wantCid) {
  if (wantCid == 0) return false;
  int len = 0;
  const uint8_t *v = adFind(p, n, 0xFF, &len);
  if (v == nullptr || len < 2) return false;
  uint16_t cid = (uint16_t)v[0] | ((uint16_t)v[1] << 8);
  return cid == wantCid;
}

// ---- 候选打分 ----
// 任何一项命中即可参选，分数只用来在同一次扫描里挑一个最像的。
#define SC_PIN   100
#define SC_NPFX   40
#define SC_VENDOR 30
#define SC_APP    25
#define SC_HIDUU  25
#define SC_ANY     1

static int scoreAdv(BLEAdvertisedDevice &dev, const char **why) {
  *why = "";
  if (!dev.isConnectable()) return 0;

  char addr[20];
  snprintf(addr, sizeof(addr), "%s", dev.getAddress().toString().c_str());

  if (g_cfg.pin[0] != 0 && strcasecmp(addr, g_cfg.pin) == 0) {
    *why = "pin"; return SC_PIN;
  }

  // 名字前缀（仅在配置了前缀时生效）
  if (g_cfg.npfx[0] != 0) {
    char nm[32];
    adName(dev.getPayload(), dev.getPayloadLength(), nm, sizeof(nm));
    if (nm[0] != 0 && strncmp(nm, g_cfg.npfx, strlen(g_cfg.npfx)) == 0) {
      *why = "npfx"; return SC_NPFX;
    }
  }

  // 厂商 ID（仅在配置了才生效）
  if (adVendorCid(dev.getPayload(), dev.getPayloadLength(), g_cfg.cid)) {
    *why = "vendor"; return SC_VENDOR;
  }

  // 通用 HID 信号
  bool hasApp = false;
  uint16_t app = adAppearance(dev.getPayload(), dev.getPayloadLength(), &hasApp);
  if (g_cfg.useApp && hasApp && app >= 0x03C0 && app <= 0x03C4) {
    *why = "appearance"; return SC_APP;
  }
  if (g_cfg.useHidUuid && adHasUuid16(dev.getPayload(), dev.getPayloadLength(), UUID_HID_SERVICE)) {
    *why = "hid-uuid"; return SC_HIDUU;
  }

  if (g_cfg.anyDev) { *why = "any"; return SC_ANY; }
  return 0;
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
    if (g_target != nullptr && g_targetScore >= SC_PIN) return;   // 已锁定地址，不必再看

    const char *why = "";
    int score = scoreAdv(dev, &why);
    if (score <= 0) return;
    if (g_target != nullptr && score <= g_targetScore) return;     // 已有更可信的候选

    Serial.printf("# 候选 %s rssi=%d 命中=%s 分数=%d\n",
                  dev.getAddress().toString().c_str(), dev.getRSSI(), why, score);
    // 注意：绝对不能在这里调 getScan()->stop()。
    // 这个回调跑在协议栈的 GAP 上下文里，在里面停扫描会把状态机卡住，
    // 之后 esp_ble_gattc_open 要等将近半分钟才发得出去（实测 31 秒才失败）。
    // 正确做法：只记下目标，让扫描自然跑完，回到 loop 再连接。
    if (g_target != nullptr) { delete g_target; g_target = nullptr; }
    g_target = new BLEAdvertisedDevice(dev);
    g_targetScore = score;
    snprintf(g_targetWhy, sizeof(g_targetWhy), "%s", why);
  }
};

// ============================================== HID Report Map 描述符解析 ====
// 目的只有一个：不写死 report id，也能知道"哪条 report 是键盘、哪条是消费者、
// 哪条是不能订的数据通道"。做法是读 0x2A4B，跟踪 Usage Page / Report ID /
// Report Size / Report Count，记下每个 report 所属的顶层集合和累计位数。
//
// 判据（都是 HID 规范里的通用含义，与设备品牌无关）：
//   顶层集合 = Generic Desktop(0x01) / Keyboard(0x06)  -> 键盘
//   顶层集合 = Consumer(0x0C) 且单帧 2 字节            -> 消费者
//   顶层集合 = Consumer(0x0C) 但单帧很长（如 255 字节） -> 数据通道，不订阅
//   其余（鼠标、厂商自定义）                            -> 忽略

struct MapEntry {
  uint8_t  page;
  uint8_t  usage;
  uint32_t bits;
  bool     seen;
};
static MapEntry g_map[256];
static uint8_t  g_mapRaw[512];      // 描述符原文，MAPDUMP 命令用
static int      g_mapLen = 0;

static bool readReportMap(BLERemoteService *hid) {
  g_mapLen = 0;
  BLERemoteCharacteristic *c = hid->getCharacteristic(BLEUUID(UUID_REPORT_MAP));
  if (c == nullptr) return false;
  String v = c->readValue();
  int n = v.length();
  if (n <= 0) return false;
  if (n > (int)sizeof(g_mapRaw)) n = sizeof(g_mapRaw);
  memcpy(g_mapRaw, v.c_str(), n);
  g_mapLen = n;
  return true;
}

static int parseReportMap(const uint8_t *d, size_t n) {
  memset(g_map, 0, sizeof(g_map));
  uint32_t page = 0, rsize = 0, rcount = 0;
  int rid = 0, depth = 0;
  uint32_t topPage = 0, topUsage = 0, lastUsage = 0;

  size_t i = 0;
  while (i < n) {
    uint8_t b = d[i];
    int nb = b & 0x03;
    if (nb == 3) nb = 4;
    if (i + 1 + nb > n) break;

    uint32_t val = 0;
    for (int k = 0; k < nb; k++) val |= (uint32_t)d[i + 1 + k] << (8 * k);

    int type = (b >> 2) & 0x03;
    int tag  = (b >> 4) & 0x0F;

    if (type == 1) {                       // Global
      if      (tag == 0x0) page  = val;     // Usage Page
      else if (tag == 0x7) rsize = val;     // Report Size
      else if (tag == 0x8) rid   = (int)val; // Report ID
      else if (tag == 0x9) rcount = val;    // Report Count
    } else if (type == 2) {                // Local
      if (tag == 0x0) lastUsage = val;      // Usage
    } else {                               // Main
      if (tag == 0x8) {                    // Input
        MapEntry &e = g_map[(uint8_t)rid];
        if (!e.seen) { e.seen = true; e.page = (uint8_t)topPage; e.usage = (uint8_t)topUsage; }
        e.bits += rsize * rcount;
        lastUsage = 0;
      } else if (tag == 0xA) {             // Collection
        if (depth == 0) { topPage = page; topUsage = lastUsage; }
        depth++;
        lastUsage = 0;
      } else if (tag == 0xC) {             // End Collection
        if (depth > 0) depth--;
        lastUsage = 0;
      } else {                             // Output / Feature
        lastUsage = 0;
      }
    }
    i += 1 + nb;
  }

  int found = 0;
  for (int r = 0; r < 256; r++) {
    if (g_map[r].seen && g_map[r].bits > 0) found++;
  }
  return found;
}

static void mapRole(uint8_t id, uint8_t *role, uint16_t *bytes) {
  MapEntry &e = g_map[id];
  if (!e.seen || e.bits == 0) { *role = ROLE_NONE; *bytes = 0; return; }
  uint16_t b = (uint16_t)((e.bits + 7) / 8);
  *bytes = b;
  if (e.page == 0x01 && e.usage == 0x06)      { *role = ROLE_KBD;  return; }
  if (e.page == 0x0C) { *role = (b == 2) ? ROLE_CONS : ROLE_DATA; return; }
  *role = ROLE_NONE;
}

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
    slot.role       = ROLE_NONE;
    slot.bytes      = 0;
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

  // ---- 自动判定每条 report 的用途（读 HID Report Map 0x2A4B）----
  bool mapOk = false;
  if (readReportMap(hid)) {
    int ents = parseReportMap(g_mapRaw, g_mapLen);
    mapOk = (ents > 0);
    Serial.printf("# Report Map: %d 字节，解析出 %d 条 report\n", g_mapLen, ents);
  } else {
    Serial.println("# Report Map 读不到（可能链路未加密），退回老规则");
  }

  for (int i = 0; i < g_reportCount; i++) {
    ReportSlot &s = g_reports[i];
    if (mapOk) {
      uint8_t role = ROLE_NONE;
      uint16_t bytes = 0;
      mapRole(s.reportId == 0xFF ? 0 : s.reportId, &role, &bytes);
      s.role = role;
      s.bytes = bytes;
    } else {
      s.role = ROLE_NONE;
      s.bytes = 0;
    }
  }

  // 逐条列出来：id / 类型 / 能否 notify / 判定出的角色
  static const char *ROLE_NAME[] = {"未知", "键盘", "消费者", "数据"};
  Serial.printf("# 共发现 %d 个 Report 特征\n", g_reportCount);
  for (int i = 0; i < g_reportCount; i++) {
    ReportSlot &s = g_reports[i];
    Serial.printf("#   id=0x%02X type=%d h=0x%04X notify=%d role=%s bytes=%u\n",
                  s.reportId, s.reportType, s.handle,
                  (s.chr && s.chr->canNotify()) ? 1 : 0,
                  ROLE_NAME[s.role & 3], s.bytes);
  }

  // ---- 订阅 ----
  // 有描述符：只订"键盘"和"单帧 2 字节的消费者"。一帧 255 字节的数据通道不订 ——
  //   实测一订数据通道，遥控器几秒内就把链路断掉；这条结论现在由 role 自动保证，
  //   不需要再写死 0xFC。
  // 没描述符：退回老规则，只订 0x01 和 0x03，行为与上一版完全一致。
  int ok = 0;
  for (int i = 0; i < g_reportCount; i++) {
    ReportSlot &s = g_reports[i];
    if (s.chr == nullptr || !s.chr->canNotify()) continue;
    if (s.reportType != 1) continue;

    bool want = mapOk ? (s.role == ROLE_KBD || s.role == ROLE_CONS)
                      : (s.reportId == 0x01 || s.reportId == 0x03);
    if (!want) continue;

    s.chr->registerForNotify(onNotify, true, true);
    Serial.printf("# 订阅 report 0x%02X (h=0x%04X, %s)\n", s.reportId, s.handle,
                  ROLE_NAME[s.role & 3]);
    ok++;
    delay(80);
  }
  return ok > 0;
}

static bool connectTarget() {
  if (g_target == nullptr) return false;

  // 扫描必须彻底停下再发起连接，否则控制器忙于扫描，连接请求会超时
  BLEDevice::getScan()->stop();
  delay(100);

  // ---- 解析要连的地址：NVS 里记住/指定的优先，其次用广播里报的 ----
  char peerAddr[20];
  if (g_cfg.pin[0] != 0) {
    snprintf(peerAddr, sizeof(peerAddr), "%s", g_cfg.pin);
  } else {
    snprintf(peerAddr, sizeof(peerAddr), "%s", g_target->getAddress().toString().c_str());
  }
  if (peerAddr[0] == 0 || strcmp(peerAddr, "00:00:00:00:00:00") == 0) {
    // 定向广播时协议栈有可能把地址报成全 0，这时没有任何线索可用
    static uint32_t lastWarn = 0;
    if (millis() - lastWarn > 10000) {
      lastWarn = millis();
      Serial.println("# !! 广播里拿不到有效地址（全 0）");
      Serial.println("#    用 TARGET xx:xx:xx:xx:xx:xx 手工指定一次，连上后会自动记住");
    }
    return false;
  }

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

    Serial.printf("# t=%lu 连接尝试 %d/2 -> %s\n", millis(), attempt, peerAddr);

    uint32_t t0 = millis();
    BLEAddress peer = BLEAddress(String(peerAddr));
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

  // 首次连上后把地址记进 NVS：下次开机直接认地址，不必再靠广播特征猜，
  // 也避免附近有别的 HID 设备时连错。
  if (g_cfg.autoPin && strcmp(g_cfg.pin, peerAddr) != 0) {
    snprintf(g_cfg.pin, sizeof(g_cfg.pin), "%.17s", peerAddr);   // 地址最长 17 字符
    saveCfg();
    Serial.printf("# 已记住设备地址 %s（TARGET CLEAR 可清除）\n", g_cfg.pin);
  }

  // 不再主动改连接参数：这句 updateConnParams 是唯一的"自伤"操作，
  // 参数单位在 NimBLE 下容易理解错，先交给协议栈默认值。
  g_lastAlive = millis();
  return true;
}

static void scanOnce() {
  // 上一轮留下的候选要释放，否则每扫一次漏一块内存
  if (g_target != nullptr) { delete g_target; g_target = nullptr; }
  g_targetScore = 0;
  g_targetWhy[0] = 0;
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

// ------------------------------------------------------ 设备识别相关命令 --
// CFG      查看 / 修改识别条件（写进 NVS，掉电不丢）
// TARGET   查看或手工指定目标地址；TARGET CLEAR 清掉记住的地址
// MAPDUMP  把读到的 Report Map 原文和解析结果打出来，换设备时用它核对
static void cmdCfg(char *args) {
  if (args != nullptr && !strcasecmp(args, "RESET")) {
    cfgDefaults();
    saveCfg();
    Serial.println("OK CFG reset");
    return;
  }
  if (args != nullptr && args[0] != 0) {
    char *p = args;
    while (p != nullptr && *p != 0) {
      char *sp = strchr(p, ' ');
      if (sp != nullptr) *sp = 0;
      char *eq = strchr(p, '=');
      if (eq != nullptr) {
        *eq = 0;
        const char *k = p;
        const char *v = eq + 1;
        if      (!strcmp(k, "pin"))      snprintf(g_cfg.pin,  sizeof(g_cfg.pin),  "%s", v);
        else if (!strcmp(k, "npfx"))     snprintf(g_cfg.npfx, sizeof(g_cfg.npfx), "%s", v);
        else if (!strcmp(k, "cid"))      g_cfg.cid        = (uint16_t)strtoul(v, nullptr, 16);
        else if (!strcmp(k, "any"))      g_cfg.anyDev     = (v[0] == '1');
        else if (!strcmp(k, "autopin"))  g_cfg.autoPin    = (v[0] == '1');
        else if (!strcmp(k, "app"))      g_cfg.useApp     = (v[0] == '1');
        else if (!strcmp(k, "hidu"))     g_cfg.useHidUuid = (v[0] == '1');
        else { Serial.printf("ERR cfg %s\n", k); return; }
      }
      if (sp == nullptr) break;
      p = sp + 1;
      while (*p == ' ') p++;
    }
    saveCfg();
  }
  Serial.printf("OK CFG pin=%s npfx=%s cid=%04X any=%d autopin=%d app=%d hidu=%d\n",
                g_cfg.pin[0] ? g_cfg.pin : "-", g_cfg.npfx[0] ? g_cfg.npfx : "-",
                g_cfg.cid, g_cfg.anyDev, g_cfg.autoPin, g_cfg.useApp, g_cfg.useHidUuid);
}

static void cmdTarget(char *args) {
  if (args != nullptr && args[0] != 0) {
    if (!strcasecmp(args, "CLEAR") || !strcasecmp(args, "NONE")) {
      g_cfg.pin[0] = 0;
      saveCfg();
      Serial.println("OK TARGET cleared");
      return;
    }
    size_t L = strlen(args);
    if (L < 11 || L > 17) { Serial.println("ERR target format"); return; }
    snprintf(g_cfg.pin, sizeof(g_cfg.pin), "%s", args);
    saveCfg();
    Serial.printf("OK TARGET %s\n", g_cfg.pin);
    return;
  }
  Serial.printf("OK TARGET pin=%s cand=%s score=%d why=%s\n",
                g_cfg.pin[0] ? g_cfg.pin : "-",
                g_target ? g_target->getAddress().toString().c_str() : "-",
                g_targetScore, g_targetWhy[0] ? g_targetWhy : "-");
}

static void cmdMapDump(char *args) {
  (void)args;
  if (g_mapLen <= 0) { Serial.println("ERR no map"); return; }
  Serial.printf("OK MAPDUMP len=%d\n", g_mapLen);
  for (int i = 0; i < g_mapLen; i++) {
    Serial.printf("%02X", g_mapRaw[i]);
    if ((i & 15) == 15) Serial.println();
  }
  if (g_mapLen & 15) Serial.println();
  for (int r = 0; r < 256; r++) {
    if (!g_map[r].seen || g_map[r].bits == 0) continue;
    uint8_t role = ROLE_NONE;
    uint16_t bytes = 0;
    mapRole((uint8_t)r, &role, &bytes);
    Serial.printf("M id=0x%02X page=0x%02X usage=0x%02X bytes=%u role=%d\n",
                  r, g_map[r].page, g_map[r].usage, bytes, role);
  }
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
  if (!strcmp(cmd, "CFG"))     { cmdCfg(args); return; }
  if (!strcmp(cmd, "TARGET"))  { cmdTarget(args); return; }
  if (!strcmp(cmd, "MAPDUMP")) { cmdMapDump(args); return; }
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
  Serial.println("# ===== CMCC 遥控器 USB 桥 v3-auto（自动识别设备/报告） =====");

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

  // ---- 设备识别条件（v3 新增）----
  loadCfg();
  Serial.printf("# 识别条件 pin=%s npfx=%s cid=%04X any=%d autopin=%d app=%d hidu=%d\n",
                g_cfg.pin[0] ? g_cfg.pin : "-", g_cfg.npfx[0] ? g_cfg.npfx : "-",
                g_cfg.cid, g_cfg.anyDev, g_cfg.autoPin, g_cfg.useApp, g_cfg.useHidUuid);

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
