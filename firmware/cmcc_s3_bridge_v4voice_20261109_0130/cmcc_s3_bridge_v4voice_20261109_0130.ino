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
 *
 * ---------------------------------------------------------------------------
 * v4（2026-10-31）语音通道版，与 v3 并存，不覆盖
 * ---------------------------------------------------------------------------
 * v3 只做按键，明确不订阅 0xFC 语音数据通道（注释理由：一订就断链）。
 * v4 把这条通道打开，目标是"按下语音键 -> 音频帧经 CDC 串口送到 PC -> 解码成 PCM"。
 *
 * 五处改动（第 4、5 条为 2026-10-31 01:20 追加）：
 *   1. 订阅逻辑允许 0xFC（及 ROLE_DATA），开关 g_audioSub，命令 AUDIOSUB 0/1。
 *   2. handleAudio 增加非阻塞保护：串口 TX 缓冲不足时丢帧而不是阻塞。
 *      GATT 回调一旦被 Serial.print 卡住，ATT 无法回确认，遥控器会主动断链——
 *      这很可能就是 v3 观察到"订了就断"的真正原因。
 *   3. 新增命令：AUDIOSUB / CONNPAR / PHY / STAT / AUTOCONN。
 *   4. setMTU 从 connect 之前挪到 connect 之后，并显式发一次 ATT_MTU_REQ。
 *      NimBLE 下库里主动交换那行是注释掉的，只设了"首选项"。
 *   5. 连上后自动请求连接参数，最终固化在 itvl=24(30ms) / latency=50 / timeout=500(5s)。
 *      这组值不是猜的：先按安卓盒子 btsnoop 里的 7.5~11.25ms / latency 0 试，
 *      实测被遥控器一律拒绝；随后把 itvl / latency / timeout 逐维扫了一遍，
 *      结论是 latency 天花板就是 50（55 及以上全拒），itvl=16 与 24 最好，
 *      timeout 只影响"断链多快被判定"，与录音时长无关，取 500。
 *      STAT 会回报控制器实际生效的 itvl/lat/to，对不上能立刻看出来。
 *
 * 帧格式（单条通知 20 字节，不含 report id）：
 *   seq(2, 小端) | idx(0/1/2) | 00 | 16 字节载荷
 *   每 3 条拼成 48 字节一帧，50 帧/秒。
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
// 位域报告的位序表。定义必须放在这里（任何函数定义之前）：
// Arduino 会把函数原型插到"第一个函数定义"之前，findBitUsage() 的返回值是
// BitUsage*，结构体若定义在它前面一点点，原型就会被插到结构体上面而编译失败
// （实测报 'BitUsage' does not name a type）。
// 位宽上限从 32 提到 128（16 字节报告）。
// 原来 32 位只够 4 字节报告，一旦某只遥控器的位域报告更长，超出部分会被
// 静默截断 —— 那些位上的键就永远识别不到。RAM 代价：每张表 256+16 字节，
// 6 张表约 1.6KB，对 S3 的 512KB 完全不是问题。
#define BITS_MAX   128
#define BITS_BYTES (BITS_MAX / 8)
struct BitUsage {
  uint8_t  id;                      // report id
  uint16_t count;                   // 总位宽（含 padding 位）
  uint16_t u[BITS_MAX];             // 该位对应的 usage；0xFFFF = 无（padding）
  uint8_t  prev[BITS_BYTES];        // 上一次的位图，用来算按下/抬起（按 id 各存一份）
};
static BitUsage g_bit[6];           // 最多记 6 条位域报告
static int      g_bitCount = 0;
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

// 固件身份：OK INFO 把 FW_NAME 原样上报，上位机直接拿它当「固件版本」显示。
// ESP 镜像里的 app descriptor 在 Arduino 构建下恒为 arduino-lib-builder + git hash，
// 从 bin 反推不出构建名，所以只能靠固件自报。
// 发版时改这里，并且和 sketch 目录名、产物文件名保持一致。
#define FW_NAME "cmcc_s3_bridge_v4voice_20261109_0130"

// ---------------------------------------------------------------- 运行开关 --
static bool g_learnMode = false;    // 学习模式：只上报 id，不发送按键
static bool g_rawMode   = false;    // 原始报告打印
static bool g_enabled   = true;     // 总开关（可以暂时禁用输出）
static bool g_audioDump = true;     // 0xFC 音频帧转储开关（AUDIO 命令）
static bool g_audioSub  = true;     // 是否订阅 0xFC 音频数据通道（AUDIOSUB 命令）
static bool g_scanDump  = false;    // 扫描时打印所有广播设备（SCANDUMP 命令）
static bool g_autoConn  = true;     // 连上后是否自动请求快速连接参数（AUTOCONN 命令）

// ------------------------------------------------------ 链路实测（GAP 事件）--
// NimBLE 下 BLEClient::getMTU() 返回的是"我们请求的值"而不是协商结果
// （setMTU 里直接 m_mtu = mtu，NimBLE 分支没有 CFG_MTU 事件回写），
// 所以真实链路参数一律从 GAP 事件里取，这里存下来给 STAT 用。
static uint16_t g_connHandle = BLE_HS_CONN_HANDLE_NONE;
static uint16_t g_lnkItvl    = 0;    // 单位 1.25ms
static uint16_t g_lnkLatency = 0;
static uint16_t g_lnkTimeout = 0;    // 单位 10ms
static bool     g_lnkValid   = false;
// DLE 协商结果（BLE_GAP_EVENT_DATA_LEN_CHG 事件里回报，不是 conn_desc 成员）
static uint16_t g_dleTx      = 0;
static uint16_t g_dleRx      = 0;
static bool     g_dleValid   = false;
// LE PHY（1M/2M/CODED）。2M 能把物理层速率翻倍，是提升音频吞吐最有依据的一招。
// 经 BLE_GAP_EVENT_PHY_UPDATE_COMPLETE 回报；也可以随时用 ble_gap_read_le_phy 查询。
static uint8_t  g_phyTx      = 0;   // 1=1M 2=2M 3=coded
static uint8_t  g_phyRx      = 0;
static bool     g_phyValid   = false;
// 遥控器改参被接受后，我们请求的 ce_len 会被 BLEClient 冲掉，
// 这里标记"需要补一次"，由 loop() 去发（不能在这个回调里发请求）。
static volatile bool g_cePending = false;
static uint32_t      g_ceAppliedAt = 0;

// ---------------------------------------------------------------- 板载灯 --
// 按键 -> 蓝色闪一下；按住语音键 -> 红色常亮。
//
// 2026-11-08：引脚和灯型改为可配置。不同 ESP32-S3 板子的板载灯 GPIO 没有统一
// 标准（实测常见 21 / 38 / 48），还有不少板根本没有 WS2812，只有一颗接 3.3V 的
// 电源指示 LED（固件控制不了）。所以两者都放进 NVS，由上位机
// 「硬件信息 → 板载灯」配置，命令 LED / LEDTEST 也能直接改。
//
// 为什么不往 DevCfg 加字段：loadCfg() 用 getBytesLength() == sizeof(g_cfg) 校验，
// 结构体一变长，老用户的整块配置（含 pin）会被直接丢弃 → 升级完就"又连不上"。
// 用两个独立的 NVS 键，老用户零影响。
static const char *LEDP_KEY = "ledp";
static const char *LEDT_KEY = "ledt";
static const uint32_t LED_TEST_MS = 3000;   // 上位机「测试亮灯」亮多久

static uint8_t  g_ledPin       = 21;   // 板载灯 GPIO，默认 21（当前这块微雪板实测值）
static uint8_t  g_ledType      = 0;    // 0=WS2812 彩灯  1=普通单色 LED
static uint8_t  g_ledState     = 0;    // 0=灭 1=蓝 2=红
static uint32_t g_ledBlueUntil = 0;    // 蓝灯闪烁截止时刻
static uint32_t g_ledTestUntil = 0;    // 测试亮灯截止时刻（非阻塞，不占死主循环）
static uint8_t  g_ledTestState = 1;    // 测试时点亮的状态：1(蓝)/2(红)，普通 LED 下都只是"亮"
static volatile bool g_voiceActive = false;   // 正在收 0xFC 数据流
static uint32_t      g_lastVoiceMs = 0;
static bool          g_voiceWasActive = false; // loop() 里检测起止变化用

// 语音键。实测 usage=0x03E（id=0x103E，走 0x01 键盘报告）。
// 之前按 0x051 是猜的，白跑一轮 —— 这个键码必须用日志实测确认，不能凭印象。
// 录音区间应该由这个键的按下/抬起界定，而不是靠"多久没收到音频帧"去猜：
// 猜出来的边界会把开头提示音和结尾残响切错，说话中间一停顿还会被劈成两段。
// 键码做成可改的（VOICEKEY 命令），万一换遥控器不必重烧。
static uint8_t g_voiceKeyCode = 0x3E;
static volatile int8_t g_voiceBtnEvent = -1;   // -1 无事件, 1 按下, 0 抬起
// 语音键是否正被按住。红灯跟这个走，不跟 g_voiceActive 走。
// g_voiceActive 是"300ms 没新帧就算结束"的推断值，音频一断灯就灭，
// 会把"遥控器停发"误显示成"已经松手"，看不出真实按键区间。
static volatile bool g_voiceHeld = false;

// 语音键按下/抬起的最后时刻。兜底解码器靠它把「语音伴随数据」排除掉：
// 三号在语音期间还会在 0xFD 上连发 20 帧 01（语音标志），那不是按键；
// 用户按住语音键说话时也不会同时按别的键，所以这段时间内的未知帧
// 只计数、不产生按键事件（松手后 800ms 内同样排除，覆盖尾巴）。
static volatile uint32_t g_lastVoiceBtnMs = 0;

// 厂商事件通道 0xF8 里「语音键」的虚拟 id。
// 真实按键 id 都落在 0x1xxx（键盘页）/ 0x2xxx（消费者页），这里用 0xF8xx 段
// 标出来源，绑定表、学习页、日志一眼能看出它不是普通 HID 键。
#define ID_VOICE_VENDOR 0xF801

// ------------------------------------------------- 前置类型定义（不许挪走）--
// 这一段必须待在「第一个函数定义」之前。Arduino 构建时用 ctags 找出所有函数
// 定义，再把原型统一插到第一个函数定义之前；若原型里出现的类型（ReportSlot*、
// GenSlot*）定义在第一个函数之后，原型就会被插到类型声明之上，编译直接报
// "'XXX' does not name a type"。BitUsage 当年已踩过一次，ReportSlot / GenSlot
// 又踩了第二次，所以这里统一收口，后面不再就地定义。
#define ROLE_NONE 0     // 认不出来（走老规则）
#define ROLE_KBD  1     // 键盘：修饰键 + 6 键码
#define ROLE_CONS 2     // 消费者控制：16 位 usage
#define ROLE_DATA 3     // 大块数据通道（0xFC 这类），一律不订阅
static const char *const ROLE_NAME[] = {"未知", "键盘", "消费者", "数据"};

// report 的角色：由 HID Report Map 描述符推导，不再靠写死 id 判断
struct ReportSlot {
  uint16_t handle;
  uint8_t  reportId;
  uint8_t  reportType;
  uint8_t  role;          // 0=未知(走老规则) 1=键盘 2=消费者 3=数据通道(不订阅)
  uint8_t  sub;           // 是否已订阅通知（GAP SUBSCRIBE 事件里更新，供 REPORTS 显示）
  uint16_t bytes;         // 描述符里算出来的单帧字节数
  BLERemoteCharacteristic *chr;
  uint8_t  last[16];      // 上一次的原始数据，用于算按下/抬起
  uint8_t  lastLen;
};
static ReportSlot g_reports[16];
static int        g_reportCount = 0;

// 未识别通道兜底解码器的状态（用法见后面 decodeGeneric）
#define GEN_MAX 8
struct GenSlot {
  uint8_t  used;
  uint8_t  id;
  uint8_t  last[16];
  uint8_t  lastLen;
  uint8_t  fast;        // 连续高频计数
  uint8_t  muted;       // 已判定为非按键通道
  uint32_t lastMs;
  uint32_t frames;
};
static GenSlot g_gen[GEN_MAX];

// ---------------------------------------------------- 开麦指令（0xFB 通道）--
// 2026-11-06：二号遥控（别人的那只）实测结论 —— 按下语音键时遥控器只发一条
// 0xF8 控制帧 + HID 键 0x3E，**不推音频**；必须由主机往 report 0xFB
// （HID 输出报告，type=2）写单字节 0x01 才开麦，写 0x00 停麦。
// 一号遥控（本机那只）不写也能出流，所以写 01 对它预期是幂等操作；
// 这一点必须实测，本版就是拿来验「是否冲突 / 是否通用」的。
//
// 通道 handle 因遥控器而异（一号 0x003B / 二号 0x0047），所以**按 reportId
// 找、不写死 handle**。找不到就不写，绝不猜。
static bool     g_voiceCtlAuto   = true;   // 按下语音键自动开/关麦（VCAUTO 命令）
static uint16_t g_voiceCtlHandle = 0;      // 已定位到的通道 handle，0 = 未找到
static int8_t   g_voiceCtlLast   = -1;     // 上一次成功写入的值（-1 = 从未写过）
static uint16_t g_voiceCtlWrites = 0;      // 累计写入次数，供 STAT/INFO 观察

// LED 颜色顺序：库里 neopixelWrite/rgbLedWrite 默认按 WS2812B 的 GRB 顺序发字节
// （rgb_led_color_order_t 默认 LED_COLOR_ORDER_GRB，见 esp32-hal-rgb-led.c）。
// 但这块微雪板子的灯实际是 RGB 顺序：传 red=60 时，库里把 red 放在第 2 字节，
// 而第 2 字节在这里是 G，结果点亮的是绿灯 —— 已实测确认。
// 所以必须显式指定 LED_COLOR_ORDER_RGB，参数语义才和"红绿蓝"对得上。
static void setLed(uint8_t s, bool force = false) {
  if (!force && g_ledState == s) return;
  g_ledState = s;
  // GPIO26~32 是 ESP32-S3 的 SPI flash / PSRAM 专用脚，对它们 pinMode/digitalWrite
  // 会直接把板子搞重启。上位机允许用户填任意数值且"填什么都要存进 NVS"，
  // 所以照样保存，但不真的去驱动。
  if (g_ledPin >= 26 && g_ledPin <= 32) return;

  if (g_ledType == 1) {
    // 普通单色 LED：没有颜色，蓝/红一律当"亮"。默认高电平点亮
    // （绝大多数板子是 GPIO -> 限流电阻 -> LED -> GND）。
    digitalWrite(g_ledPin, s == 0 ? LOW : HIGH);
    return;
  }
  if      (s == 2) rgbLedWriteOrdered(g_ledPin, LED_COLOR_ORDER_RGB, 60, 0, 0);   // 红
  else if (s == 1) rgbLedWriteOrdered(g_ledPin, LED_COLOR_ORDER_RGB, 0, 0, 60);   // 蓝
  else             rgbLedWriteOrdered(g_ledPin, LED_COLOR_ORDER_RGB, 0, 0, 0);    // 灭
}

// 普通 LED 需要先把脚配成输出；WS2812 由 rgbLedWriteOrdered 自己管。
static void ledApplyPinMode() {
  if (g_ledType != 1) return;
  if (g_ledPin >= 26 && g_ledPin <= 32) return;
  pinMode(g_ledPin, OUTPUT);
  digitalWrite(g_ledPin, LOW);
}

static void updateLed() {
  // 测试亮灯优先：上位机点「测试亮灯」后亮 LED_TEST_MS，期间不受按键/语音状态干扰。
  // 用截止时刻而不是 delay(3000) —— 干等 3 秒会把 pollSerial 和蓝牙主循环一起冻住，
  // 期间串口命令完全不响应。
  if (g_ledTestUntil != 0) {
    if (millis() < g_ledTestUntil) { setLed(g_ledTestState); return; }
    g_ledTestUntil = 0;
    setLed(0, true);                 // 测试结束先灭，下面按正常状态重算
  }
  if (g_voiceHeld) {              // 按住语音键 -> 红灯常亮，松手才灭
    setLed(2);
    g_ledBlueUntil = 0;
    return;
  }
  if (g_ledBlueUntil != 0 && millis() >= g_ledBlueUntil) g_ledBlueUntil = 0;
  setLed(g_ledBlueUntil != 0 ? 1 : 0);
}

// ---------------------------------------------------------------- 音频通道 --
// 统计：收到的帧数 / 因串口写不动而丢掉的帧数，便于判断链路是否被拖垮
static uint32_t g_audioFrames = 0;
static uint32_t g_audioDropped = 0;

// GATT 回调与串口打印之间必须解耦。
//
// 实测（2026-10-31 01:40）：同样按住 8 秒，
//   AUDIO 1（回调里直接打印 hex）：只收到 70 帧（1.4 秒）
//   AUDIO 0（不打印）           ：收到 284 帧（5.68 秒），丢 0
// 而 dropped 计数只有 8 —— 说明不是"写不进去丢了"，而是回调被拖慢后
// ATT 层回确认变慢，遥控器判定拥塞，主动停止发送。
//
// 所以回调里只做 memcpy + 入队（微秒级），真正的格式化输出交给 loop()。
// 队列按 512 帧（约 10 秒）预留，PC 端再慢也不会反压到蓝牙链路。
struct AudioFrame {
  uint8_t len;
  uint8_t data[24];
};
static QueueHandle_t g_audioQ = nullptr;
static const int AUDIO_Q_DEPTH = 512;

static void handleAudio(const uint8_t *d, size_t len) {
  g_lastVoiceMs = millis();
  g_voiceActive = true;
  g_audioFrames++;
  if (!g_audioDump) return;

  if (g_audioQ == nullptr) { g_audioDropped++; return; }
  if (len > sizeof(((AudioFrame *)0)->data)) len = sizeof(((AudioFrame *)0)->data);

  AudioFrame f;
  f.len = (uint8_t)len;
  memcpy(f.data, d, len);
  // 0 超时：队列满就立刻丢这一帧，绝不在这里等
  if (xQueueSend(g_audioQ, &f, 0) != pdTRUE) g_audioDropped++;
}

// 由 loop() 调用：把队列里的帧转成 "A <HEX>" 打到串口
static void drainAudio() {
  if (g_audioQ == nullptr) return;
  static const char HEXC[] = "0123456789ABCDEF";
  char buf[64];
  AudioFrame f;
  int budget = 64;                     // 每次 loop 最多处理 64 帧，别饿死别的任务
  while (budget-- > 0) {
    // 先 peek 再决定要不要取走。
    // 之前是"取出来 → 写不下就丢"：STAT 显示 frames=227 dropped=54，
    // 丢了 24%，而 USBCDC 的 TX FIFO 只有 64 字节，一帧 hex 就占 43 字节，
    // 稍微晚一点腾空就写不下。队列本来就是缓冲，FIFO 暂时满应该等，
    // 不该把已经收到的帧扔掉。
    if (xQueuePeek(g_audioQ, &f, 0) != pdTRUE) break;

    size_t n = 2 + (size_t)f.len * 2 + 1;        // "A " + hex + "\n"
    if (n >= sizeof(buf)) n = sizeof(buf) - 1;
    if (Serial.availableForWrite() < (int)n) break;   // 留到下次 loop 再写

    xQueueReceive(g_audioQ, &f, 0);
    size_t k = 0;
    buf[k++] = 'A';
    buf[k++] = ' ';
    for (size_t i = 0; i < f.len && k + 3 < sizeof(buf); i++) {
      buf[k++] = HEXC[(f.data[i] >> 4) & 0x0F];
      buf[k++] = HEXC[f.data[i] & 0x0F];
    }
    buf[k++] = '\n';
    if (Serial.write((const uint8_t *)buf, k) != k) g_audioDropped++;
  }
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
// ROLE_* 宏与 struct ReportSlot / g_reports 已上移到文件前部「前置类型定义」，
// 不要搬回这里（Arduino 原型注入会用到它们，见那里的说明）。

// 通知累计数。verifyIsRemote() 靠它判断"连上之后到底有没有数据进来" ——
// 遥控器不按键时不发任何通知，所以这个计数是"用户按了一下"的唯一证据。
static volatile uint32_t g_notifyCount = 0;

static BLEClient          *g_client = nullptr;
static BLEAdvertisedDevice *g_target = nullptr;
static int  g_targetScore = 0;          // 当前候选的匹配分数
static char g_targetWhy[16] = "";       // 命中的条件，便于排查
static volatile bool g_connected = false;
static volatile bool g_scanNow   = true;
static uint32_t g_nextScanAt = 0;
static uint32_t g_lastAlive = 0;

// 「SEARCH」暂停期截止时刻（millis）。非 0 且还没到点时，扫描照跑但**不连接**，
// 让被我们松开的遥控器保持广播状态，等上位机把 MAC 写回来（TARGET <mac> 会清零它）。
// 背景：上位机「扫描识别遥控」必须先把当前链路断开 —— 遥控器被连着时压根不广播，
// 固件在已连接状态下也不扫描，所以不清链就永远扫不到「换过来的那一只」。
// TARGET CLEAR 只清 NVS 里的地址，不会松链，所以另开这条命令。
static uint32_t g_holdConnectUntil = 0;

// ============================================================== 默认映射表 ==
// 出厂默认表 = 已确证的全部 29 个 BLE 键（2026-11-05 按 remotekey.ini 定型）。
//
// 为什么它这么重要：整片刷 merged.bin 从 0x0 写，NVS 区被清成 0xFF，
// setup() 里 loadBinds() 失败 → loadDefaults() + saveBinds()。也就是说
// **这张表就是「刷完开机时设备认的键位」**，跟电脑上有没有 ini 无关。
// 之前只有 28 条且缺 确定/菜单/返回/主页/*，那几个键按下会走
// handleKey() 的 `if (b == nullptr) return;` 直接没反应。
//
// 与上位机的 DEFAULTS_BUILTIN（remote_ui.py）必须逐条一致；
// 两边不同步的话「没有 ini 也能刷完即用」就不成立。
// 用户改键不影响这里：local 有 ini 时上位机会整表覆盖并 SAVE。
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
  // ---- 1.3.3 补齐：这 5 个键以前没有默认绑定，刷完按下去完全没反应 ----
  {0x2041, 1, 1, {0x28, 0, 0, 0}},              // 确定   -> 回车
  {0x1065, 1, 1, {0xE3, 0, 0, 0}},              // 菜单   -> Win
  {0x2224, 1, 1, {0x29, 0, 0, 0}},              // 返回   -> Esc
  {0x2223, 1, 1, {0xE3, 0, 0, 0}},              // 主页   -> Win
  {0x1055, 1, 1, {0x2A, 0, 0, 0}},              // "*"    -> 退格
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
    case 0xE: return "通道兜底";
    case 0xF: return (id == ID_VOICE_VENDOR) ? "厂商语音" : "厂商0xF8";
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

// 板载灯配置：两个独立的 NVS 键，不参与 DevCfg 的长度校验（理由见 g_ledPin 那段）。
static void loadLedCfg() {
  g_ledPin  = g_prefs.getUChar(LEDP_KEY, 21);
  g_ledType = g_prefs.getUChar(LEDT_KEY, 0);
  if (g_ledPin  > 48) g_ledPin  = 21;    // 越界回落默认，别用坏值去 pinMode
  if (g_ledType > 1)  g_ledType = 0;
  ledApplyPinMode();
  Serial.printf("# 板载灯 pin=%u type=%u(%s)\n",
                g_ledPin, g_ledType, g_ledType ? "普通LED" : "WS2812");
}

static void saveLedCfg() {
  g_prefs.putUChar(LEDP_KEY, g_ledPin);
  g_prefs.putUChar(LEDT_KEY, g_ledType);
}

// 一次性迁移：把后来定下来的映射补进已有的闪存表。
// 用独立 NVS 版本号 migv 控制，跑过就不再动；之后在配置端改这几个键不会被打回。
//
// 2026-10-31 22:26 修复（改自 0115 版，旧文件保留）：
//   1) 老写法 `if (saveBinds()) g_prefs.putUChar(MIG_KEY, ver);` 把版本号当成"顺手一笔"，
//      NVS 空闲页紧张时这种单键写入会静默失败（putUChar 无返回值），
//      结果 migv 永远停在旧值，每次上电都判成"没迁过"，把用户后来手工改的
//      0x1066 / 0x10CC / 0x103E 又打回迁移值。
//      现在写完立刻回读校验，不一致就等一下重试一次，仍失败打印警告。
//   2) 迁移改为"当前值已经等于目标值就一条都不写"，重复执行完全幂等，
//      也减少 NVS 擦写（擦写多了更容易触发上一条的失败）。
static const char *MIG_KEY = "migv";

// 当前表和目标完全一致时返回 false（不写 NVS），否则写入并返回 true
static bool upsertIfChanged(uint16_t id, uint8_t outType, uint8_t n, const uint8_t *keys) {
  int idx = findBind(id);
  if (idx >= 0) {
    const Bind &b = g_binds[idx];
    if (b.outType == outType && b.n == n && memcmp(b.keys, keys, n) == 0) return false;
  }
  return upsertBind(id, outType, n, keys);
}

static void applyMigrations() {
  uint8_t ver = g_prefs.getUChar(MIG_KEY, 0);
  if (ver >= 4) {
    Serial.printf("# 迁移已是最新 migv=%u，跳过\n", (unsigned)ver);
    return;
  }
  Serial.printf("# 迁移开始 migv=%u -> 4\n", (unsigned)ver);

  static const uint8_t WIN_L[] = {0xE3, 0x0F};   // 左 Win + L
  static const uint8_t WIN_H[] = {0xE3, 0x0B};   // 左 Win + H
  static const uint8_t WIN_I[] = {0xE3, 0x0C};   // 左 Win + I
  static const uint8_t CTRL_Z[]= {0xE0, 0x1D};   // 左 Ctrl + Z
  static const uint8_t DEL[]   = {0x4C};         // Delete Forward
  static const uint8_t KEY_1[] = {0x1E};         // 数字键 1

  bool changed = false;
  if (ver < 2) {
    changed |= upsertIfChanged(0x1066, 1, 2, WIN_L);   // 关机键 -> Win+L（锁屏）
    changed |= upsertIfChanged(0x10CC, 1, 1, DEL);     // "#"    -> Delete
    changed |= upsertIfChanged(0x103E, 1, 2, WIN_H);   // 录音键 -> Win+H（语音听写）
    ver = 2;
    Serial.println("# 迁移 v2：关机键->Win+L，# -> Delete，录音->Win+H");
  }
  if (ver < 3) {
    // v3：语音键由 Win+H 改成数字键 1。
    // 只改 0x103E 这一条，其余自定义绑定一律不动。
    changed |= upsertIfChanged(0x103E, 1, 1, KEY_1);
    ver = 3;
    Serial.println("# 迁移 v3：语音键 -> 数字键 1");
  }
  if (ver < 4) {
    // v4：恢复语音键 Win+H，M 键改 Ctrl+Z，设置键改 Win+I。
    // 同样只动这三条，其它自定义绑定保持原样。
    changed |= upsertIfChanged(0x103E, 1, 2, WIN_H);
    changed |= upsertIfChanged(0x20F0, 1, 2, CTRL_Z);
    changed |= upsertIfChanged(0x20EB, 1, 2, WIN_I);
    ver = 4;
    Serial.println("# 迁移 v4：语音键->Win+H，M 键->Ctrl+Z，设置键->Win+I");
  }

  // 映射有变化才落盘；但无论有没有变化都必须写版本号，
  // 否则"值已经正确但 migv 还是旧值"这种表每次上电都要再判一遍。
  if (changed && !saveBinds()) {
    Serial.println("# !! 迁移后保存映射失败，migv 不更新，下次上电重试");
    return;
  }

  // 版本号单独写，写完必须回读：NVS 页回收时这一小笔可能被静默丢弃
  g_prefs.putUChar(MIG_KEY, ver);
  if (g_prefs.getUChar(MIG_KEY, 0) != ver) {
    delay(20);
    g_prefs.putUChar(MIG_KEY, ver);                    // 页回收后重试一次
  }
  uint8_t chk = g_prefs.getUChar(MIG_KEY, 0);
  if (chk == ver) {
    Serial.printf("# 迁移完成 migv=%u ok\n", (unsigned)chk);
  } else {
    Serial.printf("# !! migv 写入失败（回读=%u 期望=%u），下次上电会重跑\n",
                  (unsigned)chk, (unsigned)ver);
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

  // 厂商 0xF8 语音键：开麦 / 红灯 / 录音边界是它的本职，跟有没有绑定无关。
  // 一号在键盘通道另有 0x3E 也会设同一个事件（值相同，幂等，不冲突）。
  if (id == ID_VOICE_VENDOR) {
    g_voiceBtnEvent = down ? 1 : 0;
    g_voiceHeld     = down;
    g_lastVoiceBtnMs = millis();     // 兜底解码器据此屏蔽语音伴随帧
  }

  if (b == nullptr) return;

  if (down) pressBind(b);
  else      releaseBind(b);
}

// ============================================================== BLE 解码 ====
// 位域式消费者报告：描述符里把每个键定义成 1 bit 的 usage。
// 三号遥控（0C:F3:10:23:14:0D）的 0x03 就是这样：
//   95 13 75 01 + 19 个 usage + 81 02  -> 19 bit
//   95 01 75 05 + 81 03                -> 5 bit padding，合计 3 字节
// 这种报告**没有 16 位 usage 字段**，必须按位查表。之前把前两个字节当
// usage 读，恒为 0，于是被当成"无按键"丢掉 —— 三号关机/确定/设置等键全无响应。
// 注：BITS_MAX / struct BitUsage / g_bit[] / g_bitCount 已上移到文件开头
// （DevCfg 之后），原因见那处注释 —— Arduino 原型插入顺序。
static BitUsage *findBitUsage(uint8_t id) {
  for (int k = 0; k < g_bitCount; k++) {
    if (g_bit[k].id == id) return &g_bit[k];
  }
  return nullptr;
}

// 表里有几个「真 usage」（非 padding）。0 表示这是一张纯占位的空表，
// 不能拿来按位解析 —— 否则 mask 恒为 0，会把整条通道的键静默丢掉。
static int bitNamed(const BitUsage *t) {
  if (t == nullptr) return 0;
  int c = 0;
  for (int b = 0; b < t->count && b < BITS_MAX; b++) {
    if (t->u[b] != 0xFFFF) c++;
  }
  return c;
}

// 键盘报告。标准布局是 mod(1) + 保留(1) + 6 个键码 = 8 字节，但各家不统一：
// 有发 3 字节简化版的，有发 16 字节带额外键位的。
// 旧实现要求 n>=8 且只读 k[2..7]，短报告直接 return（整条丢），长报告多出来
// 的字节里的键也永远读不到。现在按实际长度全量遍历，只要帧里有键码就认得出来。
//
// prev 状态按通道分表（id & 3）。原来是一份全局静态，两条键盘通道会互相踩：
// A 通道的键在 B 通道下一帧到达时被判成"已松开"，表现就是间歇性丢键/连击。
static bool    g_kbdPrev[4][256];
static uint8_t g_kbdPrevMod[4];

static void decodeKeyboard(uint8_t id, const uint8_t *d, size_t n) {
  const uint8_t *k;
  size_t kn;

  // 带 report id 前缀。判据从 n>=9 放宽到 n>=2 —— 3 字节的简化报告也可能带前缀。
  if (n >= 2 && d[0] == 0x01) { k = d + 1; kn = n - 1; }
  else                        { k = d;     kn = n; }

  if (kn == 0) return;

  const uint8_t slot = (uint8_t)(id & 3);
  bool *prev = g_kbdPrev[slot];

  // 当前按下的键码集合。kn>=3 才跳前两字节（modifier + reserved）；
  // kn==2 时第二字节就是唯一的键码（没有 reserved 的位置）；kn==1 只有 modifier。
  bool now[256] = {false};
  if (kn >= 3) {
    for (size_t i = 2; i < kn; i++) {
      if (k[i]) now[k[i]] = true;
    }
  } else if (kn == 2) {
    if (k[1]) now[k[1]] = true;
  }

  // 与上一帧比较，得到按下/抬起
  for (int c = 0; c < 256; c++) {
    if (now[c] && !prev[c]) {
      if (c == g_voiceKeyCode) {
        g_voiceBtnEvent = 1;                        // 语音键按下 -> 开始录音
        g_voiceHeld     = true;                     // 红灯开始常亮
        g_lastVoiceBtnMs = millis();
      }
      queueKey((uint16_t)(0x1000 | c), true);
    }
    if (!now[c] && prev[c]) {
      if (c == g_voiceKeyCode) {
        g_voiceBtnEvent = 0;                        // 语音键抬起 -> 结束录音
        g_voiceHeld     = false;                    // 红灯灭
        g_lastVoiceBtnMs = millis();
      }
      queueKey((uint16_t)(0x1000 | c), false);
    }
  }
  // 修饰键本身也可能是键（例如遥控器把 Shift 当普通键用）
  uint8_t mod = k[0];
  const uint8_t prevMod = g_kbdPrevMod[slot];
  for (int bit = 0; bit < 4; bit++) {
    bool a = mod     & (1 << bit);
    bool b = prevMod & (1 << bit);
    if (a && !b) queueKey((uint16_t)(0x1000 | (0xE0 + bit)), true);
    if (!a && b) queueKey((uint16_t)(0x1000 | (0xE0 + bit)), false);
  }
  g_kbdPrevMod[slot] = mod;
  memcpy(prev, now, sizeof(bool) * 256);
}

// 消费者报告，两种形态：
//   ① 16 位 usage（一号遥控）：单帧 2 字节，可能带 report id 前缀
//   ② 位域（三号遥控 0x03）：descriptor 里每个键占 1 bit，按位查 g_bit 表
//
// 2026-11-08「零静默丢弃」改造，修掉两个会整条通道丢键的判据：
//  a) 位域路径原来要求 `bt->count == n*8` **严格相等**。只要帧里多一个
//     report id 前缀、或遥控器发的比表短/长一点，就会掉到 16 位路径被读成 0，
//     然后 `if (usage == prev) return` 把整条通道静默吞掉。现在改成
//     「表里有真 usage 就按位解」，帧长按实际可用字节算，前缀自动剥离。
//  b) 16 位路径的 prev 原来是**函数级静态、不分通道** —— 两条消费者通道
//     （比如 0x03 和 0x04）会互相踩对方状态，表现为间歇性丢键。改成按 id 各存一份。
static uint16_t g_consPrev[256];        // 16 位 usage 路径上一次的值，按 report id 隔离

static void decodeConsumer(uint8_t id, const uint8_t *d, size_t n) {
  if (n == 0) return;

  // ---- 形态 ②：位域（表里有真 usage 才走这里）----
  BitUsage *bt = findBitUsage(id);
  if (bt != nullptr && bitNamed(bt) > 0) {
    const size_t need = (size_t)((bt->count + 7) / 8);   // 表宽对应的字节数
    const uint8_t *p = d;
    size_t pn = n;
    // 剥掉可选的 report id 前缀：帧正好比表宽多 1 字节、且首字节就是本报告的 id
    if (n == need + 1 && d[0] == id) { p = d + 1; pn = n - 1; }
    const size_t avail = (pn < need) ? pn : need;         // 实际可用来解位的字节数

    uint8_t m[BITS_BYTES];
    memset(m, 0, sizeof(m));
    const int lim = (bt->count < BITS_MAX) ? bt->count : BITS_MAX;
    for (int b = 0; b < lim; b++) {
      if (bt->u[b] == 0xFFFF) continue;                   // padding 位，跳过
      const size_t byi = (size_t)(b >> 3);
      if (byi >= avail) continue;                         // 这一位在帧外（帧偏短）
      if ((p[byi] >> (b & 7)) & 1) m[b >> 3] |= (uint8_t)(1u << (b & 7));
    }

    if (memcmp(m, bt->prev, BITS_BYTES) == 0) return;     // 真没变化

    for (int b = 0; b < lim; b++) {
      if (bt->u[b] == 0xFFFF) continue;
      const bool now = (m[b >> 3]        >> (b & 7)) & 1;
      const bool was = (bt->prev[b >> 3] >> (b & 7)) & 1;
      if (now && !was) queueKey((uint16_t)(0x2000 | bt->u[b]), true);
      if (!now && was) queueKey((uint16_t)(0x2000 | bt->u[b]), false);
    }
    memcpy(bt->prev, m, BITS_BYTES);
    return;
  }

  // ---- 形态 ①：16 位 usage ----
  const uint8_t *k;
  size_t kn;

  // 前缀判据用实际 id，不再写死 0x03（消费者页可能落在任意 report id 上）
  if (n >= 3 && d[0] == id) { k = d + 1; kn = n - 1; }
  else                      { k = d;     kn = n; }

  if (kn < 2) return;
  uint16_t usage = (uint16_t)k[0] | ((uint16_t)k[1] << 8);

  uint16_t *prev = &g_consPrev[id];
  if (usage == *prev) return;
  if (*prev) queueKey((uint16_t)(0x2000 | *prev), false);
  if (usage) queueKey((uint16_t)(0x2000 | usage), true);
  *prev = usage;
}

// ------------------------------------------------- 厂商事件通道 0xF8 ----
// 按语音键时这些遥控器都会在 0xF8 上发一帧：
//   82 03 XX 00 + 16 个 00，XX = 01 按下 / 00 抬起
// （一号在旧机静态逆向里确认过，二号用户日志、三号实测同构）
//
// 以前这条通道被算成 ROLE_DATA，onNotify 里直接不处理。后果按遥控器不同：
//   * 一号/二号：键盘通道另有 0x3E，语音照样能用，看不出问题
//   * 三号：**只有这一条** —— 于是按语音键既不开麦，学习模式也收不到
// 这里把它归一成一个虚拟 id 走 handleKey：学习页能捕获，
// 语音语义（开麦/红灯/录音边界）在 handleKey 里按 id 特判。
// 语音帧形态：82 03 XX 00 + 填充。不论出现在哪条通道都按这个特例先匹配，
// 它不属于「未知按键」，绝不能掉到兜底解码器里去占一个 F0xx 虚拟键。
static bool isVendorVoiceFrame(const uint8_t *d, size_t n) {
  return n >= 3 && d[0] == 0x82 && d[1] == 0x03;
}

static void decodeVendorVoice(const uint8_t *d, size_t n) {
  if (!isVendorVoiceFrame(d, n)) return;
  static int8_t prev = -1;
  int8_t cur = (d[2] != 0) ? 1 : 0;
  if (cur == prev) return;
  prev = cur;
  queueKey(ID_VOICE_VENDOR, cur != 0);
}

// ------------------------------------------------- 未知通道兜底识别 ----
// 目标：遥控器只要通过 BLE 发了通知，就不允许它静默消失。
// 语义已知的通道走各自的解析器（键盘 / 消费者 / 语音特例 / 音频），
// 剩下的私有通道（0xF9 / 0xFA / 0xFD / 0x04 / 任意自定义 id）在这里兜底：
//   帧内容一变化就产生按键事件，虚拟 id = 0xE000 | (字节位 << 8) | 字节值
// 这样学习页能学到、能绑定，日志里也能看到 `# KEY EAxx`，不再是「按了没反应、
// 日志里什么都没有」。
//
// 为什么用 0xE 段而不是 0xF 段：0xF801 已经被语音键占用，
// 如果兜底也用 0xF000|(i<<8)|value，第 8 字节从 0 变 1 就会算出 0xF801，
// 被 handleKey 当成语音键 —— 按别的键却触发开麦。0xE 段目前完全空闲，
// 范围 0xE000~0xEFFF，与键盘(0x1xxx)/消费者(0x2xxx)/语音(0xF801) 均不冲突。
//
// 两种必须排除的情况（否则会被误当按键刷屏）：
//   1. 音频 0xFC —— 50 帧/秒的连续流，由 handleAudio 处理，在 onNotify 里就分流了
//   2. 语音伴随帧 —— 语音期间/松开后 800ms 内，未知通道只计数不产键
//      （三号在 0xFD 上连发 20 帧 01 就属这类）
//
// 另外做频率闸门：同一通道 100ms 内变化超过 10 次，判定为非按键私有通道
// （心跳 / 状态推送），打印一次告警后永久静音。这是「按语音键的特例方案
// 去匹配非按键通道」的通用化：先认模式，认不出就按频率判它不可能是按键。
// GEN_MAX / struct GenSlot / g_gen 已上移到文件前部「前置类型定义」，
// 原因是 findGen() 的原型会被注入到第一个函数定义之前，这里定义会编译失败。

static GenSlot *findGen(uint8_t id) {
  for (int i = 0; i < GEN_MAX; i++) {
    if (g_gen[i].used && g_gen[i].id == id) return &g_gen[i];
  }
  for (int i = 0; i < GEN_MAX; i++) {
    if (g_gen[i].used) continue;
    memset(&g_gen[i], 0, sizeof(g_gen[i]));
    g_gen[i].used = 1;
    g_gen[i].id   = id;
    Serial.printf("# 通道 0x%02X 语义未知 -> 启用兜底识别（按键 id 落在 E0xx 段）\n", id);
    return &g_gen[i];
  }
  return nullptr;
}

static void decodeGeneric(uint8_t id, const uint8_t *d, size_t n) {
  if (n == 0) return;
  GenSlot *g = findGen(id);
  if (g == nullptr || g->muted) return;

  g->frames++;

  // 语音窗口内不产键：这段时间的未知帧是语音伴随数据，不是按键
  if (g_voiceHeld || (millis() - g_lastVoiceBtnMs) < 800) return;

  // 频率闸门：100ms 内变化超过 10 次 -> 非按键通道
  uint32_t now = millis();
  if (now - g->lastMs < 100) {
    if (g->fast < 255) g->fast++;
    if (g->fast > 10) {
      g->muted = 1;
      Serial.printf("# !! 通道 0x%02X 变化过频（>10 次/100ms）-> 判定为非按键通道"
                    "（心跳/状态推送），已停止解析（累计 %u 帧）\n",
                    id, (unsigned)g->frames);
      return;
    }
  } else {
    g->fast = 0;
  }
  g->lastMs = now;

  const size_t m = (n < 16) ? n : 16;     // id 里只编得下 4 bit 字节位
  for (size_t i = 0; i < m; i++) {
    const uint8_t nowV = d[i];
    const uint8_t oldV = (i < g->lastLen) ? g->last[i] : 0;
    if (nowV == oldV) continue;
    const uint16_t idOld = (uint16_t)(0xE000 | ((i & 0x0F) << 8) | oldV);
    const uint16_t idNew = (uint16_t)(0xE000 | ((i & 0x0F) << 8) | nowV);
    if (oldV) queueKey(idOld, false);
    if (nowV) queueKey(idNew, true);
  }
  memcpy(g->last, d, m);
  g->lastLen = (uint8_t)m;
}

// 通道首次收帧诊断。排障时最要紧的是「这一帧到底收到没有、拿什么解的」，
// 旧版只能开 RAW 看原始字节，或者什么都看不到。
static uint8_t g_chSeen[256];

static const char *channelHow(const ReportSlot *slot, const uint8_t *d, size_t n) {
  const uint8_t id = slot->reportId;
  if (id == 0xFC)                           return "音频";
  if (isVendorVoiceFrame(d, n))             return "厂商语音";
  if (slot->role == ROLE_KBD || id == 0x01) return "键盘";
  if (slot->role == ROLE_CONS || id == 0x03) return "消费者";
  return "兜底";
}

// 通知分发的唯一入口。原则：**任何一帧都必须有明确去向，绝不静默丢弃**。
// 旧版最后一行是「其余（0xF8/0xF9/鼠标/厂商自定义）不处理」，
// 任何落在未列出 id 上的按键都会人间蒸发 —— 这就是「按键无响应」的来源。
static void onNotify(BLERemoteCharacteristic *chr, uint8_t *data, size_t len, bool isNotify) {
  g_notifyCount++;

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

  const uint8_t id = slot->reportId;

  if (!g_chSeen[id]) {
    g_chSeen[id] = 1;
    Serial.printf("# CH 0x%02X 首帧 len=%u role=%s -> 按%s解析\n",
                  id, (unsigned)len, ROLE_NAME[slot->role & 3],
                  channelHow(slot, data, len));
  }

  // ---- 唯一的非按键通道：音频。绝不进按键流程 ----
  if (id == 0xFC) {
    handleAudio(data, len);
    return;
  }

  // ---- 厂商语音帧：不论落在哪条通道，先按特例匹配掉 ----
  // 语音键不能走兜底，否则会额外冒出一个 F0xx 虚拟键。
  if (isVendorVoiceFrame(data, len)) {
    decodeVendorVoice(data, len);
    return;
  }

  // ---- 语义已知的按键通道 ----
  if (slot->role == ROLE_KBD || id == 0x01) {
    decodeKeyboard(id, data, len);
    return;
  }
  if (slot->role == ROLE_CONS || id == 0x03) {
    decodeConsumer(id, data, len);
    return;
  }

  // ---- 其余私有通道：兜底识别，不再有「不处理」这条出口 ----
  decodeGeneric(id, data, len);
}

// ------------------------------------------------- 开麦指令（0xFB 输出报告）--
// 只认 reportId == 0xFB，**不做"任意可写输出报告"的退路**：
// 一号遥控同时有 0xFB(t2 h=0x003B) 和 0xFA(t2 h=0x004A) 两条输出报告，
// 盲选会写错通道，可能变成音量/换台之类的操作。找不到就报告并跳过。
static BLERemoteCharacteristic *findVoiceCtlChar() {
  for (int i = 0; i < g_reportCount; i++) {
    ReportSlot &s = g_reports[i];
    if (s.chr == nullptr)          continue;
    if (s.reportId != 0xFB)        continue;
    if (!s.chr->canWrite() && !s.chr->canWriteNoResponse()) continue;
    return s.chr;
  }
  return nullptr;
}

static void voiceCtl(uint8_t on) {
  if (g_client == nullptr || !g_client->isConnected()) return;

  BLERemoteCharacteristic *c = nullptr;
  if (g_voiceCtlHandle != 0) {                     // 复用上次定位到的通道
    for (int i = 0; i < g_reportCount; i++) {
      if (g_reports[i].handle == g_voiceCtlHandle) { c = g_reports[i].chr; break; }
    }
  }
  if (c == nullptr) {
    c = findVoiceCtlChar();
    if (c == nullptr) {
      Serial.println("# VOICECTL 跳过：HID 报告里没有可写的 report 0xFB");
      return;
    }
    g_voiceCtlHandle = c->getHandle();
    Serial.printf("# VOICECTL 通道锁定 h=0x%04X (report 0xFB)\n", g_voiceCtlHandle);
  }

  uint8_t  b        = on ? 0x01 : 0x00;
  bool     withResp = c->canWrite();               // 有 Write 属性就走带响应写
  uint32_t t0       = millis();
  c->writeValue(&b, 1, withResp);                  // 单字节：01 开麦 / 00 停麦
  uint32_t dt       = millis() - t0;

  g_voiceCtlLast = (int8_t)(on ? 1 : 0);
  g_voiceCtlWrites++;
  // 耗时打出来：正常应 < 100ms。若接近 ATT 超时（30s），说明遥控器不认这条写，
  // 那就是"不通用"的直接证据，别去怀疑音频链路。
  Serial.printf("# VOICECTL %02X -> h=0x%04X resp=%d %lums writes=%u\n",
                b, c->getHandle(), withResp ? 1 : 0,
                (unsigned long)dt, (unsigned)g_voiceCtlWrites);

  // 熔断：这条写是本版唯一的风险点。若某只遥控器对这个 handle 不响应，
  // 带响应写会一直等到 ATT 超时，把 loop 卡住、音频全丢。
  // 一旦发现异常耗时，立刻关掉自动开麦，宁可没有这功能也不能拖垮原有链路。
  if (dt > 3000) {
    g_voiceCtlAuto = false;
    Serial.println("# VOICECTL 写入耗时异常，已自动关闭 VCAUTO（排查后可用 VCAUTO 1 重开）");
  }
}

// ============================================================== BLE 连接 ====
// 抓断开原因：遥控器到底为什么把我们踢掉
static int gapLog(struct ble_gap_event *ev, void *arg) {
  switch (ev->type) {
    case BLE_GAP_EVENT_CONNECT:
      Serial.printf("# GAP CONNECT status=%d\n", ev->connect.status);
      if (ev->connect.status == 0) {
        g_connHandle = ev->connect.conn_handle;
        // 把初始参数也记一份：后面 CONNREQ 生效后再打一次，两者一对比就知道
        // 请求到底有没有被接受。
        struct ble_gap_conn_desc desc;
        if (ble_gap_conn_find(g_connHandle, &desc) == 0) {
          g_lnkItvl    = desc.conn_itvl;
          g_lnkLatency = desc.conn_latency;
          g_lnkTimeout = desc.supervision_timeout;
          g_lnkValid   = true;
          Serial.printf("# GAP INITIAL itvl=%u (%.2fms) lat=%u to=%u (%ums)\n",
                        g_lnkItvl, g_lnkItvl * 1.25, g_lnkLatency,
                        g_lnkTimeout, g_lnkTimeout * 10);
        }
      }
      break;
    case BLE_GAP_EVENT_DISCONNECT:
      Serial.printf("# GAP DISCONNECT reason=0x%02X\n", ev->disconnect.reason);
      g_connHandle = BLE_HS_CONN_HANDLE_NONE;
      g_lnkValid = false;
      break;
    case BLE_GAP_EVENT_CONN_UPDATE:
      // NimBLE 这个事件只回 status 和 conn_handle，不带新参数。
      // 头文件原话："the connection's descriptor reflects the updated parameters"，
      // 所以要查连接描述符才能拿到实际生效值。
      if (ev->conn_update.status == 0) {
        struct ble_gap_conn_desc desc;
        if (ble_gap_conn_find(ev->conn_update.conn_handle, &desc) == 0) {
          g_lnkItvl    = desc.conn_itvl;
          g_lnkLatency = desc.conn_latency;
          g_lnkTimeout = desc.supervision_timeout;
          g_lnkValid   = true;
          Serial.printf("# GAP CONNUPD itvl=%u (%.2fms) lat=%u to=%u (%ums)\n",
                        g_lnkItvl, g_lnkItvl * 1.25, g_lnkLatency,
                        g_lnkTimeout, g_lnkTimeout * 10);
        } else {
          Serial.println("# GAP CONNUPD ok 但查不到描述符");
        }
      } else {
        Serial.printf("# GAP CONNUPD FAILED status=%d\n", ev->conn_update.status);
      }
      break;
    case BLE_GAP_EVENT_DATA_LEN_CHG:
      // DLE 协商结果经由此事件回报（Arduino 下 RX 被 core 锁 27，TX 应随我们的请求变 251）。
      g_dleTx    = ev->data_len_chg.max_tx_octets;
      g_dleRx    = ev->data_len_chg.max_rx_octets;
      g_dleValid = true;
      Serial.printf("# DLE chg tx_octets=%u rx_octets=%u\n", g_dleTx, g_dleRx);
      break;
    case BLE_GAP_EVENT_PHY_UPDATE_COMPLETE:
      // PHY 切换结果。status!=0 表示对端不接受（比如遥控不支持 2M），此时 tx/rx 无意义。
      if (ev->phy_updated.status == 0) {
        g_phyTx    = ev->phy_updated.tx_phy;
        g_phyRx    = ev->phy_updated.rx_phy;
        g_phyValid = true;
        Serial.printf("# PHY chg tx=%u rx=%u (%s)\n", g_phyTx, g_phyRx,
                      g_phyTx == 2 ? "2M" : (g_phyTx == 1 ? "1M" : "coded"));
      } else {
        Serial.printf("# PHY update FAILED status=%d（对端不接受该 PHY）\n",
                      ev->phy_updated.status);
      }
      break;
    // 注意：CONN_UPDATE_REQ 刻意不在这里处理。
    //
    // BLEDevice::gapEventHandler() 是 void 函数，它先在内部调用
    // BLEClient::handleGAPEvent()（那个返回值才是 NimBLE 认的），
    // 然后才轮到我们这个自定义处理器，且**把返回值直接丢掉**：
    //     m_pClient->handleGAPEvent(event, param);      // 真正的裁决
    //     m_customGapHandler(event, param);             // 我们，返回值无效
    // 所以在这里 return 任何错误码都不可能拦住从机改参。
    //
    // 真正有效的钩子是 BLEClientCallbacks::onConnParamsUpdateRequest()，
    // 库自己的代码就是靠它决定接受/拒绝：
    //     rc = ...onConnParamsUpdateRequest(...) ? 0 : BLE_ERR_CONN_PARMS;
    // 详见下面 ClientHandler 的实现。
    case BLE_GAP_EVENT_ENC_CHANGE:
      Serial.printf("# GAP ENC status=%d\n", ev->enc_change.status);
      break;
    case BLE_GAP_EVENT_SUBSCRIBE: {
      uint16_t subH = ev->subscribe.attr_handle;
      Serial.printf("# GAP SUBSCRIBE attr=0x%04X cur=%d\n",
                    subH, ev->subscribe.cur_notify);
      // 记下真实的订阅状态，REPORTS 的 sub= 才有意义（以前那两项写死成恒 1）
      for (int i = 0; i < g_reportCount; i++) {
        if (g_reports[i].handle == subH) {
          g_reports[i].sub = ev->subscribe.cur_notify ? 1 : 0;
          break;
        }
      }
      break;
    }
    default:
      break;
  }
  return 0;
}
// 通用连接参数请求。
//
// 为什么单独抽出来：除了间隔/latency，还有两个一直被忽略的字段
//   min_ce_len / max_ce_len  —— 连接事件长度，单位 0.625ms。
//
// BLE 里从机只能紧跟在主机的包之后发送，所以一个连接事件里从机能发几个包，
// 取决于主机愿意把这个事件开多久。换算成实测数据：
//   盒子：150 条通知/秒 ÷ 100 事件/秒 = 1.5 包/事件
//   我们： 35 条通知/秒 ÷ 100 事件/秒 ≈ 0.35 包/事件
// 而 ce_len 在代码和库默认值里都是 0x0000（等于"不主张"），
// 很可能是从机因此每个事件只敢塞一个包、甚至塞不进。
//
// 注意一个坑：BLEClient::handleGAPEvent() 接受从机改参时，只把
// itvl_min/itvl_max/latency/supervision_timeout 写回 self_params
// （BLEClient.cpp:1266-1271），**不写 ce_len**。所以我们在连上时请求的
// ce_len，会在遥控器随后发来的改参被接受时被冲掉，必须之后再补一次。
static bool requestConnParams(uint16_t itvl_min, uint16_t itvl_max,
                              uint16_t lat, uint16_t timeout,
                              uint16_t min_ce, uint16_t max_ce,
                              const char *why) {
  if (g_client == nullptr || !g_client->isConnected()) return false;
  if (g_connHandle == BLE_HS_CONN_HANDLE_NONE) return false;

  ble_gap_upd_params p;
  memset(&p, 0, sizeof(p));
  p.itvl_min            = itvl_min;
  p.itvl_max            = itvl_max;
  p.latency             = lat;
  p.supervision_timeout = timeout;
  p.min_ce_len          = min_ce;
  p.max_ce_len          = max_ce;

  int rc = ble_gap_update_params(g_connHandle, &p);
  Serial.printf("# CONNREQ %s itvl=%u~%u lat=%u to=%u ce=%u~%u rc=%d\n",
                why, itvl_min, itvl_max, lat, timeout, min_ce, max_ce, rc);
  return rc == 0;
}

// 固化连接参数（2026-11-01 实测最优：itvl=24 / latency=50）：
//   interval=24 (30ms) / latency=50 / supervision timeout=500 (5s)。
// 实测结论：录音时长主因是 itvl（遥控器对 24/32 档倒流窗口最佳），latency=50
// 是被遥控器接受的最高可用值（>50 全被拒）。itvl=24/lat=50 是稳定录 3~5s 的最优配置。
//   - itvl=16 是谷底（lat=50 也仅 0.86s）；itvl=9+lat=50 仅 1.04s
//   - itvl=24/lat=50 多轮 1.78~5.28s（中位 ~3.4s），远优于默认 9/0 的 0.9s
//   - timeout 与录音时长无关（400~1600 同区间），取 500(5s) 稳妥且合法
//   - 注意 BLE 约束：timeout > (1+latency)*itvl*2 = 51*30ms*2 = 3.06s，故 to 不得低于 ~307
// ce_len 直接一次带 0xFFFF——因 onConnParamsUpdateRequest 已拒绝遥控改参，
// 不会被库冲掉（旧版需要先发 0 再补一次，现在不用了）。
static bool applyFastConn() {
  return requestConnParams(24, 24, 50, 500, 0, 0xFFFF, "fast");
}

// 补一次 ce_len。
//
// 用遥控器自己想要的间隔/latency 去请求，只把 ce_len 放大，
// 这样它没有理由拒绝，而我们能把"连接事件能开多长"这件事表态出去。
// 一旦生效，从机在一个事件里就有机会连发多个通知，这正是盒子
// 1.5 包/事件 与我们 0.35 包/事件 的差距所在。
static bool applyBigCeLen() {
  uint16_t itvl = g_lnkValid ? g_lnkItvl : 8;
  uint16_t lat  = g_lnkValid ? g_lnkLatency : 99;
  uint16_t to   = g_lnkValid ? g_lnkTimeout : 500;
  if (itvl < 6 || itvl > 3200) itvl = 8;
  if (to < 10 || to > 3200) to = 500;
  // 0xFFFF = 不设上限，实际长度由间隔本身约束（10ms 最多 16 个 slot）
  return requestConnParams(itvl, itvl, lat, to, 0, 0xFFFF, "bigce");
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

// 2026-11-06 23:10 回退：曾试过"pin 连续失败几轮就作废、改按打分选设备"，
// 实测无效 —— 本机扫到的遥控器广播地址恒为 00:00:00:00:00:00（见 23:03 日志），
// 打分选出来的候选也没有可用地址，反而把 pin 锁死这条路一起丢了。
// 恢复 1.3.4 行为：有 pin 就用 pin，没有才用广播里的地址。

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

// ------------------------------------------------- 首次使用：候选穷举验证 --
// 背景（四号用户反馈）：新用户 NVS 里没有 pin，只能靠「扫到 → 打分 → 连上」。
// 但广播里的判据全都没有排他性（Appearance 0x03C1 是 HID Keyboard 的官方值，
// 任何蓝牙键盘都命中），扫到之后仍然不知道哪个才是遥控器。
//
// 所以真正可靠的判据只有一个：**连上之后看 Report Map**。做法是
//   收集本轮所有得了分的候选（按 rssi 从强到弱）→ 逐个连上验证 → 第一个通过的
//   自动写进 pin。
// 一旦有了 pin，完全走原来的直连逻辑 —— 日常使用行为零变化，这条边界很重要：
// 穷举只在"从没连上过任何设备"时才启用，不会去抢用户已经用惯的那只。
#define MAX_CANDS      6        // 一轮最多收集的候选数
#define VERIFY_WAIT_MS 1600     // 连上后等多久看有没有报告进来（按一下键就到）
#define MAX_BLACK      8        // 本次运行内的失败黑名单容量

static BLEAdvertisedDevice *g_cands[MAX_CANDS] = {nullptr};
static int  g_candCount = 0;
static char g_black[MAX_BLACK][18];
static int  g_blackCount = 0;

static void candClear() {
  for (int i = 0; i < MAX_CANDS; i++) {
    if (g_cands[i] != nullptr) { delete g_cands[i]; g_cands[i] = nullptr; }
  }
  g_candCount = 0;
}

// 收一个候选。同地址只留 rssi 更好的那条；队列满时替换掉最弱的一条，
// 保证留下的是"最强的 N 个"。全 0 地址直接不收 —— 那个地址连不上。
static void candAdd(BLEAdvertisedDevice &dev) {
  char addr[20];
  snprintf(addr, sizeof(addr), "%s", dev.getAddress().toString().c_str());
  if (strcmp(addr, "00:00:00:00:00:00") == 0) return;

  for (int i = 0; i < g_candCount; i++) {
    if (g_cands[i] == nullptr) continue;
    if (strcasecmp(g_cands[i]->getAddress().toString().c_str(), addr) == 0) {
      if (dev.getRSSI() > g_cands[i]->getRSSI()) {
        delete g_cands[i];
        g_cands[i] = new BLEAdvertisedDevice(dev);
      }
      return;
    }
  }

  if (g_candCount < MAX_CANDS) {
    g_cands[g_candCount++] = new BLEAdvertisedDevice(dev);
    return;
  }
  int worst = -1, worstR = 0;
  for (int i = 0; i < g_candCount; i++) {
    if (g_cands[i] == nullptr) continue;
    if (worst < 0 || g_cands[i]->getRSSI() < worstR) { worst = i; worstR = g_cands[i]->getRSSI(); }
  }
  if (worst >= 0 && dev.getRSSI() > worstR) {
    delete g_cands[worst];
    g_cands[worst] = new BLEAdvertisedDevice(dev);
  }
}

static void candSortByRssi() {
  for (int i = 0; i < g_candCount; i++) {
    for (int j = i + 1; j < g_candCount; j++) {
      if (g_cands[i] == nullptr || g_cands[j] == nullptr) continue;
      if (g_cands[j]->getRSSI() > g_cands[i]->getRSSI()) {
        BLEAdvertisedDevice *t = g_cands[i];
        g_cands[i] = g_cands[j];
        g_cands[j] = t;
      }
    }
  }
}

static bool blackHas(const char *addr) {
  for (int i = 0; i < g_blackCount; i++) {
    if (!strcasecmp(g_black[i], addr)) return true;
  }
  return false;
}

// 只在本次运行内记着（不写 NVS）：重上电就忘，不影响下次开机重新试。
static void blackAdd(const char *addr) {
  if (blackHas(addr)) return;
  if (g_blackCount >= MAX_BLACK) {
    memmove(g_black[0], g_black[1], sizeof(g_black[0]) * (MAX_BLACK - 1));
    g_blackCount = MAX_BLACK - 1;
  }
  snprintf(g_black[g_blackCount], sizeof(g_black[0]), "%.17s", addr);
  g_blackCount++;
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

    // 只有"还没记住任何设备"（首次使用）时才收集候选队列。
    // 已经有 pin 时走直连，队列既没用、又让回调白拷一份对象。
    if (g_cfg.pin[0] == 0) candAdd(dev);

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

// 单例回调对象。
// 原来 scanOnce() 里写的是 setAdvertisedDeviceCallbacks(new ScanHandler(), false)，
// 每扫一次就 new 一个、谁都不 delete —— 断链重扫频繁时跑几小时会积累上千个
// 泄漏对象，把堆切碎，表现为"跑久了变卡 / 莫名重启"。对象本身无状态，
// 一份静态实例就够，注册的是它的地址，生命周期贯穿整个程序。
static ScanHandler g_scanHandler;

// 背景与结论（2026-10-31 01:55 实测更正）：
// 遥控器连上后会主动发 L2CAP Connection Parameter Update Request，
// 把链路改到 itvl=9 (11.25ms) / latency=99。
//
// 曾经一度认为 latency=99 是"数据滞后"的元凶，于是在这里一律拒绝、强留
// latency=0。实测证明这个判断是错的，而且是有害的：
//
//   同样 AUDIO 0（串口全程不输出）、同样按一次语音键：
//     接受遥控器参数(lat=99) -> 173 条通知 = 57 帧 = 1.15 秒音频
//     拒绝、强留 lat=0        -> 更少，且遥控器约 0.7 秒后彻底停发
//   （更早一版注释里记录的 284 帧，也是在接受参数的前提下测到的）
//
// 盒子的 HCI 一手记录同样如此：终态 11.25ms / lat=99，
// 而它的 hidraw 抓包里是 257 帧、seq 39280..39536 完全连续、0 丢失，
// 正好 5.14 秒 —— 50 帧/秒满速。所以 lat=99 并不妨碍满速传输，
// 它是从机攒批发送的许可，遥控器靠它才能把音频连续灌出来。
//
// 因此这里一律接受，行为与盒子完全一致。只做记录，不做决策。
// 真正的低延迟问题（松手后数据才到齐）留到 PC 端用"等流自然结束"来解决，
// 不能靠牺牲链路吞吐来换。
class ClientHandler : public BLEClientCallbacks {
 public:
  void onConnect(BLEClient *c) override {
    Serial.printf("# CB onConnect conn_id=%d\n", c ? c->getConnId() : -1);
  }

  void onDisconnect(BLEClient *c) override {
    Serial.printf("# CB onDisconnect conn_id=%d\n", c ? c->getConnId() : -1);
  }

  bool onConnParamsUpdateRequest(BLEClient *c, const ble_gap_upd_params *p) override {
    if (p == nullptr) return false;
    Serial.printf("# CB CONNUPD_REQ itvl=%u~%u (%.2f~%.2fms) lat=%u to=%u (%ums) -> 拒绝（保留固化 itvl=9/lat=0）\n",
                  p->itvl_min, p->itvl_max, p->itvl_min * 1.25f, p->itvl_max * 1.25f,
                  p->latency, p->supervision_timeout, p->supervision_timeout * 10);
    // 方案A（2026-11-01）：拒绝遥控器的改参请求。否则它的 itvl=8/lat=99 会盖掉
    // 我们 central 请求的 itvl=9/lat=0，且 g_lnkLatency=99 之后会被 applyBigCeLen 重发。
    // 拒绝后链路保持固化的 itvl=24/lat=50/to=500；ce_len 由 applyFastConn 一次性带 0xFFFF，无被冲风险。
    return false;
  }
};
static ClientHandler g_clientHandler;

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
  g_bitCount = 0;
  uint32_t page = 0, rsize = 0, rcount = 0;
  int rid = 0, depth = 0;
  uint32_t topPage = 0, topUsage = 0, lastUsage = 0;
  // 一条报告里可能连着声明多个 Usage，每个键一个，得按顺序攒下来。
  // 以前只留最后一个（lastUsage），位域报告需要完整序列。
  uint32_t local[96];
  int localN = 0;
  bool localFull = false;      // 只告警一次，别刷屏

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
      if (tag == 0x0) {                    // Usage
        lastUsage = val;
        if (localN < (int)(sizeof(local) / sizeof(local[0]))) {
          local[localN++] = val;
        } else if (!localFull) {
          localFull = true;
          Serial.printf("# !! 描述符一条报告里声明了超过 %d 个 usage，超出部分被忽略\n",
                        (int)(sizeof(local) / sizeof(local[0])));
        }
      }
    } else {                               // Main
      if (tag == 0x8) {                    // Input
        MapEntry &e = g_map[(uint8_t)rid];
        if (!e.seen) { e.seen = true; e.page = (uint8_t)topPage; e.usage = (uint8_t)topUsage; }
        e.bits += rsize * rcount;

        // 「1 bit 一个 usage」的报告，把位序记下来，供 decodeConsumer 按位查表。
        // 无 usage 的常量项（padding / 保留字节）也必须占位，否则总位宽对不上：
        //   95 13 75 01 + 19 usage + 81 02  -> 19 bit
        //   95 01 75 05 + 81 03             -> 5 bit padding（此时 rsize 已是 5，不是 1）
        //
        // **新建表的门槛是「本项逐条列出了 usage」（rsize==1 && localN>0）。**
        // 常量填充只能追加到已经存在的表上，绝不能自己开一张新表 ——
        // 一号 0x03 就是 rsize=16/rcount=1/localN=0 的 16 位 usage 报告
        // （描述符用 19 00 / 2A FF 03 的范围写法，不算逐条 usage），
        // 旧条件 `localN == 0` 也建表，结果造出一张 16 位纯 padding 的空表：
        // 按位解析 mask 恒 0、与 prev 相等直接 return，于是
        // M / 音量+ / 音量- / 返回 / 确定 / 设置 / 静音 七个键全部静默丢弃。
        if (rcount > 0) {
          const bool create = (rsize == 1 && localN > 0);
          BitUsage *t = findBitUsage((uint8_t)rid);
          if (t == nullptr && create &&
              g_bitCount < (int)(sizeof(g_bit) / sizeof(g_bit[0]))) {
            t = &g_bit[g_bitCount++];
            t->id = (uint8_t)rid;
            t->count = 0;
            memset(t->prev, 0, sizeof(t->prev));
            for (int k = 0; k < BITS_MAX; k++) t->u[k] = 0xFFFF;
          }
          // create 时表是刚建的空表；否则只允许往「已有真 usage 的表」追加 padding
          if (t != nullptr && (create || bitNamed(t) > 0)) {
            if (rsize == 1) {
              for (uint32_t k = 0; k < rcount && t->count < BITS_MAX; k++) {
                t->u[t->count++] = (k < (uint32_t)localN)
                                     ? (uint16_t)(local[k] & 0xFFFF) : 0xFFFF;
              }
            } else {
              // rsize>1 且没有 usage：常量填充，按位占位（典型是 1 字节保留）
              uint32_t bits = rcount * rsize;
              for (uint32_t k = 0; k < bits && t->count < BITS_MAX; k++) t->u[t->count++] = 0xFFFF;
            }
          }
        }

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
      localN = 0;                          // 每个 Main 项结束，Local 状态清空
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
  // 消费者页一律按 ROLE_CONS 处理，不再按「正好 2 字节」区分。
  // 旧判据 `b == 2` 把 3 字节位域报告（三号）判成 ROLE_DATA，靠 onNotify 里
  // `reportId == 0x03` 特例才兜住；换个 id（比如 0x04 也做消费者通道）就直接
  // 落到「不处理」分支。字节数只影响解析路径，不影响"这是消费者通道"这个事实。
  if (e.page == 0x0C) { *role = ROLE_CONS; return; }
  *role = ROLE_NONE;
}

// ---------------------------------------------------------------- 电池 --
// 遥控器带标准 Battery Service（0x180F），Battery Level 特征（0x2A19）
// 在 handle 0x0010，属性 0x12 = READ|NOTIFY。这两个 UUID 和 handle 不是猜的，
// 是从盒子 btsnoop 的服务发现响应里直接读出来的：
//   svc 0x000E~0x0011  Battery Service
//   decl=0x000F value=0x0010 props=0x12  Battery Level
// 注意 BLE 库的 BLEUUID.h 里没有 UUID_BATTERY 常量，必须用数值构造。
static uint8_t g_batteryPct   = 0xFF;    // 0xFF = 未知
static bool    g_batteryValid = false;
static uint32_t g_lastBattMs  = 0;

static bool readBattery() {
  if (g_client == nullptr || !g_client->isConnected()) return false;

  BLERemoteService *bat = g_client->getService(BLEUUID((uint16_t)0x180F));
  if (bat == nullptr) {
    Serial.println("# 没发现电池服务 0x180F（可能被加密要求挡住）");
    return false;
  }
  BLERemoteCharacteristic *lvl = bat->getCharacteristic(BLEUUID((uint16_t)0x2A19));
  if (lvl == nullptr) {
    Serial.println("# 没发现 Battery Level 0x2A19");
    return false;
  }

  String v = lvl->readValue();
  if (v.length() < 1) {
    Serial.println("# 读电量为空（链路未加密时常见）");
    return false;
  }
  uint8_t pct = (uint8_t)v.c_str()[0];    // 标准单位就是 0~100 的百分比
  g_batteryPct   = (pct <= 100) ? pct : 100;
  g_batteryValid = true;
  g_lastBattMs   = millis();
  Serial.printf("OK BATT %u%%\n", (unsigned)g_batteryPct);
  return true;
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
  g_voiceCtlHandle = 0;   // 每次重新发现都作废：换遥控器 / 重连后 handle 会变
  for (auto &kv : *chars) {
    BLERemoteCharacteristic *c = kv.second;
    if (!c->getUUID().equals(BLEUUID(UUID_REPORT))) continue;
    // 表满时必须吼一声：不订阅的通道永远收不到数据，那条通道上的键会
    // 100% 无响应，而且日志里看不出任何痕迹。实测一号/三号都是 9 条，
    // 容量 16 有余量；真撞上了要能从日志一眼看到。
    if (g_reportCount >= (int)(sizeof(g_reports) / sizeof(g_reports[0]))) {
      Serial.printf("# !! HID report 特征超过 %d 条，多出的通道不会被订阅"
                    "（那些通道上的键会无响应）\n",
                    (int)(sizeof(g_reports) / sizeof(g_reports[0])));
      break;
    }

    ReportSlot &slot = g_reports[g_reportCount];
    slot.handle     = c->getHandle();
    slot.chr        = c;
    slot.reportId   = 0xFF;
    slot.reportType = 0;
    slot.role       = ROLE_NONE;
    slot.sub        = 0;
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
  // （ROLE_NAME 是文件级常量表，定义在前置类型定义区，不在这里重复声明）
  Serial.printf("# 共发现 %d 个 Report 特征\n", g_reportCount);
  for (int i = 0; i < g_reportCount; i++) {
    ReportSlot &s = g_reports[i];
    Serial.printf("#   id=0x%02X type=%d h=0x%04X notify=%d role=%s bytes=%u\n",
                  s.reportId, s.reportType, s.handle,
                  (s.chr && s.chr->canNotify()) ? 1 : 0,
                  ROLE_NAME[s.role & 3], s.bytes);
  }

  // 位域表：打印出来才能一眼判断"消费者报告是按位解析还是按 16 位 usage 解析"。
  // 正常只有三号那种 1-bit 报告会出现在这里；一号 0x03 若出现且 named=0，
  // 说明建表条件又写错了（整条消费者通道会被吞掉）。
  for (int k = 0; k < g_bitCount; k++) {
    Serial.printf("#   位表 id=0x%02X 总位=%u 有效=%d\n",
                  g_bit[k].id, g_bit[k].count, bitNamed(&g_bit[k]));
  }

  // ---- 订阅 ----
  // v4：语音数据通道（0xFC）改为可订阅，由 AUDIOSUB 命令控制，默认开。
  //
  // v3 里写的是"一订数据通道遥控器几秒内就把链路断掉"，据此永久不订。
  // 但那只遥控器在安卓盒子上一按就能连续送出 257 帧（5.14 秒），说明设备本身
  // 愿意持续发；断链更可能是主机侧卡住导致的。最可疑的是 handleAudio 里的
  // Serial.print：没有主机读串口时 TX 缓冲写满会阻塞 GATT 回调，ATT 层无法
  // 回确认，遥控器等不到就断开。v4 已给那段加了"缓冲不足即丢帧"的保护。
  // 若实测仍断链，发 AUDIOSUB 0 可立刻退回 v3 行为，不必重烧。
  int ok = 0;
  for (int i = 0; i < g_reportCount; i++) {
    ReportSlot &s = g_reports[i];
    if (s.chr == nullptr || !s.chr->canNotify()) continue;
    if (s.reportType != 1) continue;

    // 除了 0xFC 由 AUDIOSUB 单独控制，其余能通知的 report 一律订上 ——
    // 与盒子完全一致。盒子 HCI 一手记录里的 CCCD 写操作有 8 条：
    //   0x0030(0x01) 0x0034(0xFC) 0x0038(0xFD) 0x003F(0x03)
    //   0x0043(0xF9) 0x0047 0x004E 0x0053(这份是 indication)
    // 也就是说它把遥控器的报告通道几乎全订了，而只发 1 秒左右就停的那台主机
    // （我们之前）只订了 3 个。这是目前唯一还没对齐的差异，所以先补上。
    //
    // 关于"多订会不会污染音频"：不会。onNotify 已经是按 report id 精确分流，
    // 只有 0xFC 进 handleAudio，0xF8/0xF9 只是被收下、不参与拼帧。
    // 之前那个 seq 跳变问题出在"按 role 分发"，那个逻辑已经删掉了。
    bool want = (s.reportId == 0xFC) ? g_audioSub : true;
    if (!want) continue;

    s.chr->registerForNotify(onNotify, true, true);
    Serial.printf("# 订阅 report 0x%02X (h=0x%04X, %s)\n", s.reportId, s.handle,
                  ROLE_NAME[s.role & 3]);
    ok++;
    delay(80);
  }
  return ok > 0;
}

// 连一个具体地址，成功后把 MTU / 连接参数 / HID / 电量全部走完。
// maxAttempts 由调用方决定：没有备选设备时给 2 次（失败后趁热重试，遥控器
// 按键唤醒后只广播很短一段），有备选时给 1 次，省下 5 秒去试下一台。
static bool connectAddr(const char *peerAddr, int maxAttempts) {
  // 每次尝试都用全新的 client：上一轮失败后 gattc 注册状态会残留，
  // 复用同一个 client 会让注册信号量白等很久（实测一次失败拖到 31 秒）
  bool ok = false;
  for (int attempt = 1; attempt <= maxAttempts && !ok; attempt++) {
    if (g_client != nullptr) {
      delete g_client;
      g_client = nullptr;
    }
    g_client = BLEDevice::createClient();
    if (g_client == nullptr) {
      Serial.println("# createClient 失败");
      return false;
    }
    // 必须在 connect() 之前注册：改参请求可能紧跟链路建立就发过来，
    // 晚一步注册就会漏掉第一条，而第一条恰恰就是把我们顶到 lat=99 的那条。
    g_client->setClientCallbacks(&g_clientHandler);

    Serial.printf("# t=%lu 连接尝试 %d/%d -> %s\n", millis(), attempt, maxAttempts, peerAddr);

    uint32_t t0 = millis();
    BLEAddress peer = BLEAddress(String(peerAddr));
    ok = g_client->connect(peer, 0, 5000);
    uint32_t dt = millis() - t0;
    Serial.printf("# t=%lu 连接%s 耗时=%lu ms\n", millis(), ok ? "成功" : "失败", (unsigned long)dt);

    // 失败后立刻重试：遥控器按键唤醒后只广播很短一段时间，趁热连才有机会
    if (!ok) delay(150);
  }
  if (!ok) return false;

  // ---- MTU：必须放在 connect 之后 ----
  // v4 之前把 setMTU(247) 写在 connect 之前，是无效的：
  //   1) BLEClient::setMTU() 要拿 m_conn_id 才能向对端发请求，连接前它是空的；
  //   2) connect() 内部会 esp_ble_gattc_app_register 重新分配接口，把之前的设置冲掉。
  // 而且 NimBLE 分支里库里那行主动交换是被注释掉的，只设了"首选项"，
  // 所以这里额外显式发一次 ATT_MTU_REQ。
  g_client->setMTU(247);
  if (g_connHandle != BLE_HS_CONN_HANDLE_NONE) {
    int rcM = ble_gattc_exchange_mtu(g_connHandle, nullptr, nullptr);
    Serial.printf("# MTU req=247 exchange rc=%d\n", rcM);
  } else {
    Serial.println("# !! 拿不到 conn_handle，跳过 MTU 交换");
  }

  // ---- 连接参数：对齐安卓盒子上的实测值 ----
  // 盒子的 HCI 日志实测到三档：7.5ms(lat0) -> 45ms(lat0) -> 11.25ms(lat99)。
  // 这里请求 7.5~11.25ms 且 latency=0，是语音流最有利的姿态。
  // 先等 150ms 让加密/服务发现前的链路稳定，太早发容易被控制器拒。
  if (g_autoConn) { delay(150); applyFastConn(); }

  // 注意：这里曾经主动发起过配对（BLESecurity::startSecurity），
  // 结果链路在 5.5 秒后被遥控器断掉。原生是"连上就能用"，
  // 说明遥控器不希望被强制走安全流程，所以这里不做任何配对动作。

  if (!setupHid()) {
    Serial.println("# !! 连上了但读不到可订阅的 report（多半是配对/加密没完成）");
    g_client->disconnect();
    return false;
  }

  // ---- DLE 探测（2026-10-31 21:04 加，可回滚）----
  // 主动请求 LL data length 协商：声明我们想用 251 字节 TX。
  // 目的不是让 Arduino 下收大包（controller 的 MAX_RX 被 core 锁死 27 字节），
  // 而是看遥控器是否"认识"DLE 协议——它若回 LL_LENGTH_RSP，串口 HCI 日志里
  // 就会出现 LE_Data_Length_Change 事件（subevent 0x0A），证明它支持 DLE，
  // ESP-IDF 开 MAX_RX 才有戏；若毫无反应/断链，说明它不支持，ESP-IDF 也没用。
  if (g_connHandle != BLE_HS_CONN_HANDLE_NONE) {
    int rcD = ble_gap_set_data_len(g_connHandle, 251, 2120);
    Serial.printf("# DLE req tx_octets=251 tx_time=2120 rc=%d\n", rcD);
  }

  // 电量：放在 setupHid 之后读，因为加密是在服务发现过程中完成的，
  // 太早读 0x2A19 会被回 Insufficient Authentication 而拿不到值。
  // 失败不影响主流程（语音才是主业），只是 STAT 里 batt 保持 -1。
  readBattery();
  return true;
}

// ---- 连接成功后统一收尾：记住地址（autoPin）+ 复位保活计时 ----
static bool finishConnected(const char *peerAddr) {
  // 首次连上后把地址记进 NVS：下次开机直接认地址，不必再靠广播特征猜，
  // 也避免附近有别的 HID 设备时连错。
  if (g_cfg.autoPin && strcmp(g_cfg.pin, peerAddr) != 0) {
    snprintf(g_cfg.pin, sizeof(g_cfg.pin), "%.17s", peerAddr);   // 地址最长 17 字符
    saveCfg();
    Serial.printf("# 已记住设备地址 %s（TARGET CLEAR 可清除）\n", g_cfg.pin);
  }
  g_lastAlive = millis();
  return true;
}

// 连上之后判断"这是不是遥控器"。广播层做不到这件事（Appearance 0x03C1 是
// 官方 HID Keyboard 值，任何蓝牙键盘都命中），只有连上看到 Report Map 才行。
//
// 强证据（命中即算）：report 0xFC —— 语音裸管道，一号/三号都有，普通键盘不可能有；
// 消费者控制报告 0x03 或 role=消费者 —— 键盘不会发音量/返回这类 usage。
// 兜底：先没有强证据的话，等 VERIFY_WAIT_MS 看有没有任何通知（用户按键时必有）。
static bool verifyIsRemote() {
  bool hasCons = false;
  for (int i = 0; i < g_reportCount; i++) {
    ReportSlot &s = g_reports[i];
    if (s.reportId == 0xFC) {
      Serial.println("# 验证：有 report 0xFC 语音通道 -> 是遥控器");
      return true;
    }
    if (s.reportId == 0x03 || s.role == ROLE_CONS) hasCons = true;
  }
  if (hasCons) {
    Serial.println("# 验证：有消费者控制报告 -> 是遥控器");
    return true;
  }

  uint32_t n0 = g_notifyCount;
  uint32_t t0 = millis();
  while (millis() - t0 < VERIFY_WAIT_MS) {
    if (g_notifyCount != n0) {
      Serial.printf("# 验证：%lu ms 内收到 %u 条通知 -> 认为是遥控器\n",
                    (unsigned long)(millis() - t0), (unsigned)(g_notifyCount - n0));
      return true;
    }
    delay(40);
  }
  Serial.println("# 验证：没有 0xFC / 消费者报告，也没收到任何通知 -> 不是遥控器");
  return false;
}

// 连"本轮最该连的那台"。
//   ① NVS 里有 pin：完全走原来的直连逻辑（与 1.3.4 等价），日常使用零变化。
//   ② NVS 里没有 pin：首次使用，把本轮候选逐个连上验证，第一个通过的写进 pin。
// 这条边界很重要：穷举只在"从没连上过任何设备"时启用，不会去抢用户用惯的那只。
static bool connectTarget() {
  // 扫描必须彻底停下再发起连接，否则控制器忙于扫描，连接请求会超时
  BLEDevice::getScan()->stop();
  delay(100);

  // ============ 一、NVS 里有地址：完全走原来的直连逻辑（回归安全）============
  if (g_cfg.pin[0] != 0) {
    if (g_target == nullptr) return false;      // 这一轮没扫到候选就先不连
    char peerAddr[20];
    snprintf(peerAddr, sizeof(peerAddr), "%s", g_cfg.pin);
    Serial.printf("# 连接目标 %s（记住的地址）\n", peerAddr);
    if (!connectAddr(peerAddr, 2)) return false;
    return finishConnected(peerAddr);
  }

  // ============ 二、NVS 里没有地址：首次使用，逐个连上验证 ============
  // 新用户手上没有任何历史记录，必须靠这一轮扫到的候选自己找出来。
  // 判据只能连上之后拿（见 verifyIsRemote 的注释），所以这里会真的去连
  // 附近每一个"HID 设备"，连错就断开换下一个。
  candSortByRssi();
  if (g_candCount == 0) {
    // 全 0 地址会被 candAdd 挡在队列外，所以到这里说明这一轮真没有可用候选
    static uint32_t lastZero = 0;
    if (millis() - lastZero > 10000) {
      lastZero = millis();
      Serial.println("# 这一轮没有任何候选（先按一下遥控器按键唤醒它）");
    }
    return false;
  }

  Serial.printf("# 首次使用：本轮 %d 个候选，逐个连上验证\n", g_candCount);
  int tried = 0;
  for (int i = 0; i < g_candCount; i++) {
    if (g_cands[i] == nullptr) continue;
    char peerAddr[20];
    snprintf(peerAddr, sizeof(peerAddr), "%s",
             g_cands[i]->getAddress().toString().c_str());
    int rssi = g_cands[i]->getRSSI();

    if (blackHas(peerAddr)) {
      Serial.printf("# 候选 %d/%d %s 本次已试过并失败，跳过\n", i + 1, g_candCount, peerAddr);
      continue;
    }

    Serial.printf("# 候选 %d/%d %s rssi=%d 尝试连接\n", i + 1, g_candCount, peerAddr, rssi);
    tried++;
    if (!connectAddr(peerAddr, 1)) {
      Serial.printf("# 候选 %s 连不上，换下一个\n", peerAddr);
      blackAdd(peerAddr);
      continue;
    }
    if (!verifyIsRemote()) {
      Serial.printf("# 候选 %s 连上了但不是遥控器，断开换下一个\n", peerAddr);
      blackAdd(peerAddr);
      if (g_client != nullptr && g_client->isConnected()) g_client->disconnect();
      g_reportCount = 0;
      delay(150);
      continue;
    }
    Serial.printf("# 已确认是遥控器：%s\n", peerAddr);
    return finishConnected(peerAddr);
  }

  static uint32_t lastFail = 0;
  if (millis() - lastFail > 10000) {
    lastFail = millis();
    if (tried == 0) {
      Serial.println("# 本轮没有可尝试的候选（都在黑名单里），等下一轮重新扫");
    } else {
      Serial.println("# 所有候选都没通过验证。请按住遥控器上的键再等一轮"
                     "（按键时才会广播，也才有报告）");
    }
  }
  return false;

}

static void scanOnce() {
  // 上一轮留下的候选要释放，否则每扫一次漏一块内存
  if (g_target != nullptr) { delete g_target; g_target = nullptr; }
  candClear();                 // 首次使用的候选队列同样每轮重建
  g_targetScore = 0;
  g_targetWhy[0] = 0;
  BLEScan *scan = BLEDevice::getScan();
  scan->setAdvertisedDeviceCallbacks(&g_scanHandler, false);   // 静态实例，不再每次 new
  // 【2026-11-08 19:24 试探版】被动 → 主动扫描。
  // 诱因：一号遥控在 ESP32 上广播地址恒为 00:00:00:00:00:00，而同一时刻 Windows 能读到
  // 0C:F3:CC:63:10:E3（12 秒 42 条）。payload 完好、库只是原样取 HCI 事件的 addr、
  // NVS 绑定记录干净、同轮扫描别的设备地址正常 ⇒ 只剩 controller 环节可疑。
  // 主动扫描会发 SCAN_REQ、让 controller 走另一条状态机（SCAN_RSP 处理路径），
  // 用它来判断「换个扫描方式能不能把地址带出来」。除这一行外本版与 1845 完全一致。
  scan->setActiveScan(true);      // 主动扫描：发 SCAN_REQ，顺带收 SCAN_RSP
  // 【2026-11-08 20:05 占空比试探版】唯一改动：30/300（10% 占空比）→ 100/100（连续扫描）。
  // 诱因：第二块板（34:B7:DA:59:5F:28）烧同一份 1930 固件扫 25 秒，一号遥控一条都没收到，
  // 而同一时段 Windows 收到 62 条（rssi=-23）。算下来原配置每 1.3 秒只有 100ms 在真正接收
  // = 7.7% 覆盖率，短窗口广播被大面积漏掉；30ms 窄窗边界也可能导致 controller 来不及填 AdvA
  // （就是「地址全 0」）。本版只用于验证「占空比是不是根因」，不进版本序列。
  scan->setInterval(100);         // 100ms 周期全开窗 = 100% 占空比（连续扫描）
  scan->setWindow(100);
  scan->start(SCAN_SECONDS, false);
  scan->clearResults();
}

// ============================================================== 串口协议 ====
// 全部十六进制不带 0x，行尾 \n。日志行以 '#' 开头，exe 忽略即可。
static void cmdInfo() {
  Serial.printf("OK INFO name=%s ver=%d binds=%d learn=%d enabled=%d connected=%d dirty=%d audio=%d\n",
                FW_NAME, MAP_VERSION, g_bindCount, g_learnMode ? 1 : 0,
                g_enabled ? 1 : 0, g_connected ? 1 : 0, g_dirty ? 1 : 0,
                g_audioDump ? 1 : 0);
  // 开麦指令（0xFB）状态。上位机按 key 名取值，多这行不影响老版本 exe。
  Serial.printf("OK VCCTL auto=%d h=0x%04X last=%d writes=%u\n",
                g_voiceCtlAuto ? 1 : 0, g_voiceCtlHandle,
                (int)g_voiceCtlLast, (unsigned)g_voiceCtlWrites);
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
    // 写回地址就结束 SEARCH 暂停期，立刻按这个地址去连（扫描识别遥控的最后一步靠它）。
    g_holdConnectUntil = 0;
    g_scanNow = true;
    g_nextScanAt = millis();
    Serial.printf("OK TARGET %s\n", g_cfg.pin);
    return;
  }
  Serial.printf("OK TARGET pin=%s cand=%s score=%d why=%s\n",
                g_cfg.pin[0] ? g_cfg.pin : "-",
                g_target ? g_target->getAddress().toString().c_str() : "-",
                g_targetScore, g_targetWhy[0] ? g_targetWhy : "-");
}

// ---- 板载灯配置 ----
// LED                  查当前值
// LED <pin> <type>     只保存，不亮灯
// LEDTEST <pin> <type> 保存 + 立刻亮 LED_TEST_MS（上位机「测试亮灯」用这条）
//
// type: 0 = WS2812 彩灯（点亮时亮蓝），1 = 普通单色 LED（点亮时就是亮）
// pin 接受 0~48 任意值：用户可能填错，但"点了测试就要存进 NVS"是明确要求，
// 所以这里只挡明显非法的（超范围/非数字），不判断"这个脚上到底有没有灯"。
static void cmdLed(char *args, bool test) {
  if (args == nullptr || args[0] == 0) {
    Serial.printf("OK LED pin=%u type=%u name=%s test=%d\n",
                  g_ledPin, g_ledType, g_ledType ? "LED" : "WS2812",
                  g_ledTestUntil ? 1 : 0);
    return;
  }

  // 拆成 "<pin> <type>"
  char *sp = strchr(args, ' ');
  const char *p1 = args;
  const char *p2 = nullptr;
  if (sp != nullptr) {
    *sp = 0;
    p2 = sp + 1;
    while (*p2 == ' ') p2++;
  }

  if (p1[0] < '0' || p1[0] > '9') { Serial.println("ERR led pin"); return; }
  int pin = atoi(p1);
  if (pin < 0 || pin > 48) { Serial.println("ERR led pin"); return; }

  int type = g_ledType;
  if (p2 != nullptr && p2[0] != 0) {
    if (p2[0] != '0' && p2[0] != '1') { Serial.println("ERR led type"); return; }
    type = p2[0] - '0';
  }

  uint8_t oldPin = g_ledPin;
  uint8_t oldType = g_ledType;

  g_ledPin  = (uint8_t)pin;
  g_ledType = (uint8_t)type;
  saveLedCfg();          // 不论引脚对不对都写 NVS —— 这是"测试即保存"的要求

  // 换脚前先把老引脚的普通 LED 关掉，免得在原地留下一个常亮灯
  if (oldType == 1 && oldPin != g_ledPin &&
      !(oldPin >= 26 && oldPin <= 32)) {
    digitalWrite(oldPin, LOW);
  }
  ledApplyPinMode();

  if (test) {
    g_ledTestState = 1;                    // WS2812 亮蓝；普通 LED 就是点亮
    g_ledTestUntil = millis() + LED_TEST_MS;
    setLed(g_ledTestState, true);          // force：立刻生效，不等下一轮 updateLed
  } else {
    g_ledTestUntil = 0;
    setLed(0, true);
  }

  Serial.printf("OK %s pin=%u type=%u name=%s ms=%u\n",
                test ? "LEDTEST" : "LED", g_ledPin, g_ledType,
                g_ledType ? "LED" : "WS2812",
                test ? (unsigned)LED_TEST_MS : 0u);
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
                    (s.chr && s.chr->canNotify()) ? 1 : 0, s.sub ? 1 : 0);
    }
    return;
  }
  if (!strcmp(cmd, "CFG"))     { cmdCfg(args); return; }
  if (!strcmp(cmd, "TARGET"))  { cmdTarget(args); return; }
  if (!strcmp(cmd, "LED"))     { cmdLed(args, false); return; }
  if (!strcmp(cmd, "LEDTEST")) { cmdLed(args, true);  return; }
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
  if (!strcmp(cmd, "AUDIOSUB")) {
    g_audioSub = (args != nullptr && args[0] == '1');
    Serial.printf("OK AUDIOSUB %d 订阅=%d（需重新连接生效，发 REBOOT 或按一下遥控器重连）\n",
                  g_audioSub ? 1 : 0, g_audioSub ? 1 : 0);
    return;
  }
  if (!strcmp(cmd, "CONNPAR")) {
    // CONNPAR <intervalMin> <intervalMax> <latency> <timeout>
    // NimBLE 单位：interval 1.25ms，timeout 10ms。例：CONNPAR 12 24 0 400 = 15~30ms/延迟0/4秒
    int a = 0, b = 0, c = 0, dly = 0;
    if (args == nullptr || sscanf(args, "%d %d %d %d", &a, &b, &c, &dly) != 4) {
      Serial.println("ERR 用法 CONNPAR <min> <max> <latency> <timeout>");
      return;
    }
    if (g_client == nullptr || !g_client->isConnected()) { Serial.println("ERR 未连接"); return; }
    g_client->updateConnParams((uint16_t)a, (uint16_t)b, (uint16_t)c, (uint16_t)dly);
    Serial.printf("OK CONNPAR %d %d %d %d\n", a, b, c, dly);
    return;
  }
  if (!strcmp(cmd, "PHY")) {
    // PHY [1|2|coded]  不带参数=只查询当前 PHY；带参数=请求切换。
    // 切换是"请求"，对端（遥控器）可以不接受；结果看 # PHY chg 或再查一次。
    if (g_connHandle == BLE_HS_CONN_HANDLE_NONE) { Serial.println("ERR 未连接"); return; }

    if (args == nullptr || args[0] == '\0') {
      uint8_t tx = 0, rx = 0;
      int rc = ble_gap_read_le_phy(g_connHandle, &tx, &rx);
      if (rc != 0) { Serial.printf("ERR 读 PHY 失败 rc=%d\n", rc); return; }
      g_phyTx = tx; g_phyRx = rx; g_phyValid = true;
      Serial.printf("OK PHY tx=%u rx=%u (%s)\n", tx, rx,
                    tx == 2 ? "2M" : (tx == 1 ? "1M" : "coded"));
      return;
    }
    uint8_t mask = 0;
    if (args[0] == '1')      mask = BLE_GAP_LE_PHY_1M_MASK;
    else if (args[0] == '2') mask = BLE_GAP_LE_PHY_2M_MASK;
    else if (args[0] == 'c' || args[0] == 'C') mask = BLE_GAP_LE_PHY_CODED_MASK;
    else { Serial.println("ERR 用法 PHY [1|2|coded]，不带参数=查询"); return; }

    // 第 4 个参数 phy_opts 只对 CODED 有意义（S2/S8），1M/2M 用 ANY(0) 即可。
    uint16_t opts = (mask == BLE_GAP_LE_PHY_CODED_MASK) ? BLE_GAP_LE_PHY_CODED_S8
                                                        : BLE_GAP_LE_PHY_CODED_ANY;
    int rc = ble_gap_set_prefered_le_phy(g_connHandle, mask, mask, opts);
    Serial.printf("%s PHY set mask=0x%02x rc=%d（结果见 # PHY chg，或再发 PHY 查询）\n",
                  rc == 0 ? "OK" : "ERR", mask, rc);
    return;
  }
  if (!strcmp(cmd, "BATT")) {
    // 手动读一次电量。遥控器 0x2A19 属性含 NOTIFY，但我们不做订阅——
    // 电量变化几分钟才一次，轮询读比常驻通知更省事，也不占链路。
    if (g_client == nullptr || !g_client->isConnected()) { Serial.println("ERR 未连接"); return; }
    if (!readBattery() && g_batteryValid) {
      Serial.printf("OK BATT %u%%（本次读取失败，显示上次缓存值）\n", (unsigned)g_batteryPct);
    } else if (!g_batteryValid) {
      Serial.println("ERR 读不到电量");
    }
    return;
  }
  if (!strcmp(cmd, "STAT")) {
    Serial.printf("OK STAT frames=%lu dropped=%lu conn=%d reports=%d voice=%d\n",
                  (unsigned long)g_audioFrames, (unsigned long)g_audioDropped,
                  g_connected ? 1 : 0, g_reportCount, g_voiceActive ? 1 : 0);
    // 开麦指令的即时状态（上位机的 STAT 解析按 key 名取，多出这两行不影响它）
    Serial.printf("OK VCCTL auto=%d h=0x%04X last=%d writes=%u\n",
                  g_voiceCtlAuto ? 1 : 0, g_voiceCtlHandle,
                  (int)g_voiceCtlLast, (unsigned)g_voiceCtlWrites);
    // batt=-1 表示还没读到（未连接或链路未加密）；0~100 才是真实百分比
    Serial.printf("OK BATT %d\n", g_batteryValid ? (int)g_batteryPct : -1);
    // 链路明细：itvl/lat/to 来自 GAP CONN_UPDATE 事件，是控制器回报的"实际生效值"。
    // mtu 用 ble_att_mtu() 取真实协商值 —— BLEClient::getMTU() 在 NimBLE 下
    // 只是把 setMTU 的参数原样存进 m_mtu，永远返回我们请求的数，不可信。
    uint16_t mtuReal = 0;
    if (g_connHandle != BLE_HS_CONN_HANDLE_NONE) mtuReal = ble_att_mtu(g_connHandle);
    if (g_lnkValid) {
      Serial.printf("OK LINK itvl=%u (%.2fms) lat=%u to=%u (%ums) mtu=%u\n",
                    g_lnkItvl, g_lnkItvl * 1.25, g_lnkLatency,
                    g_lnkTimeout, g_lnkTimeout * 10, mtuReal);
    } else {
      Serial.printf("OK LINK no-data（还没收到 CONN_UPDATE 事件）mtu=%u\n", mtuReal);
    }
    // DLE 协商结果（经 BLE_GAP_EVENT_DATA_LEN_CHG 事件存入全局变量）：
    // Arduino 下 max_rx_octets 被 core 锁 27，max_tx_octets 应随我们的请求(251)变。
    // 真正判断遥控器是否支持 DLE，要看串口 HCI 日志里的 LE_Data_Length_Change（subevent 0x0A）。
    if (g_dleValid) {
      Serial.printf("OK DLE max_tx_octets=%u max_rx_octets=%u (Arduino 下 RX 锁 27)\n",
                    g_dleTx, g_dleRx);
    } else {
      Serial.println("OK DLE (尚未收到 DATA_LEN_CHG 事件)");
    }
    // LE PHY：1=1M（默认）/2=2M（速率翻倍）/3=coded（远距离，速率反而更低）
    if (g_phyValid) {
      Serial.printf("OK PHY tx=%u rx=%u (%s)\n", g_phyTx, g_phyRx,
                    g_phyTx == 2 ? "2M" : (g_phyTx == 1 ? "1M" : "coded"));
    } else {
      Serial.println("OK PHY unknown（发 PHY 查询一次）");
    }
    return;
  }
  if (!strcmp(cmd, "AUTOCONN")) {
    g_autoConn = (args != nullptr && args[0] == '1');
    Serial.printf("OK AUTOCONN %d（下次连接生效）\n", g_autoConn ? 1 : 0);
    return;
  }
  if (!strcmp(cmd, "VC")) {
    // VC 1 -> 立刻写 01 开麦（不按语音键也能验）；VC 0 -> 写 00 停麦
    // VC   -> 只查询
    int v = -1;
    if (args != nullptr) sscanf(args, "%d", &v);
    if (v == 0 || v == 1) {
      voiceCtl((uint8_t)v);
      Serial.printf("OK VC %d\n", v);
      return;
    }
    Serial.printf("OK VC auto=%d h=0x%04X last=%d writes=%u（用法：VC 0|1，VCAUTO 0|1）\n",
                  g_voiceCtlAuto ? 1 : 0, g_voiceCtlHandle,
                  (int)g_voiceCtlLast, (unsigned)g_voiceCtlWrites);
    return;
  }
  if (!strcmp(cmd, "VCAUTO")) {
    // 是否在语音键按下/抬起时自动开/关麦。关掉后只能用 VC 手动写。
    g_voiceCtlAuto = (args != nullptr && args[0] == '1');
    Serial.printf("OK VCAUTO %d\n", g_voiceCtlAuto ? 1 : 0);
    return;
  }
  if (!strcmp(cmd, "VCINFO")) {
    // 只定位不写：确认这只遥控的 0xFB 在哪、可写属性如何
    BLERemoteCharacteristic *c = findVoiceCtlChar();
    if (c == nullptr) {
      Serial.println("OK VCINFO none  没有可写的 report 0xFB（开麦指令不可用）");
      return;
    }
    g_voiceCtlHandle = c->getHandle();
    Serial.printf("OK VCINFO h=0x%04X write=%d writeNR=%d notify=%d\n",
                  c->getHandle(), c->canWrite() ? 1 : 0,
                  c->canWriteNoResponse() ? 1 : 0, c->canNotify() ? 1 : 0);
    return;
  }
  if (!strcmp(cmd, "VOICEKEY")) {
    // VOICEKEY 3E  -> 把语音键设成 usage 0x3E（默认值，实测）
    // VOICEKEY     -> 只查询当前值
    int v = 0;
    if (args != nullptr && sscanf(args, "%x", &v) == 1 && v > 0 && v <= 0xFF) {
      g_voiceKeyCode = (uint8_t)v;
    }
    Serial.printf("OK VOICEKEY 0x%02X（用法：VOICEKEY 3E）\n", g_voiceKeyCode);
    return;
  }
  if (!strcmp(cmd, "SCANDUMP")) {
    g_scanDump = (args != nullptr && args[0] == '1');
    Serial.printf("OK SCANDUMP %d\n", g_scanDump ? 1 : 0);
    return;
  }
  // 断链 + 暂停自动重连。上位机「扫描识别遥控」的第一步就是发它。
  // 用法：SEARCH [秒]，默认 20 秒（上位机抓 15 秒广播，留出余量）。
  // 期间扫到候选也**不连**，只把关单打出来，让上位机从 `# ADV` 里挑地址；
  // 上位机随后发 `TARGET <mac>` 就会清掉暂停期、立刻去连那一个。
  if (!strcmp(cmd, "SEARCH")) {
    uint32_t secs = 20;
    if (args != nullptr) sscanf(args, "%u", &secs);
    if (secs < 5)   secs = 5;
    if (secs > 180) secs = 180;
    g_holdConnectUntil = millis() + secs * 1000UL;
    bool wasConn = g_connected;
    if (g_client != nullptr && g_connected) g_client->disconnect();
    // 松链后把这些状态一起清掉：否则红灯会一直亮、语音也会被当成还没结束
    g_connected   = false;
    g_reportCount = 0;
    g_voiceActive = false;
    g_voiceHeld   = false;
    Keyboard.releaseAll();
    g_scanNow    = true;
    g_nextScanAt = millis();
    Serial.printf("OK SEARCH hold=%lu wasconn=%d\n", (unsigned long)secs, wasConn ? 1 : 0);
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
  // 这块板子（USBMode=default + CDCOnBoot=cdc）的 Serial 是 USBCDC(TinyUSB)，
  // 没有 setTxBufferSize()（那是 HWCDC 的接口），TX FIFO 大小由
  // CFG_TUD_CDC_TX_BUFSIZE 在预编译库中固定，改不了。
  //
  // 但 USBCDC::write 每次进去都要 xSemaphoreTake(tx_lock, tx_timeout_ms)，
  // 默认超时 100ms —— 也就是 FIFO 一满，写调用会在 GATT 回调里干等 100ms。
  // 把超时设成 0 走非阻塞路径：FIFO 满就立刻返回已写入的字节数，绝不等待。
  Serial.setTxTimeoutMs(0);
  delay(600);

  // 音频中转队列：512 帧 × 25 字节 ≈ 12.8KB，约 10 秒缓冲。
  // GATT 回调只往这里塞，loop() 慢慢往串口倒。
  g_audioQ = xQueueCreate(AUDIO_Q_DEPTH, sizeof(AudioFrame));
  if (g_audioQ == nullptr) Serial.println("# !! 音频队列创建失败，语音会大量丢帧");
  else                     Serial.printf("# 音频队列已建：%d 帧缓冲\n", AUDIO_Q_DEPTH);

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
  loadLedCfg();

  g_evtQueue = xQueueCreate(32, sizeof(KeyEvent));

  // ---- 蓝牙 ----
  // 打开协议栈原始日志：连接失败时能看到 HCI 层的 status（0x3E 超时 / 0x05 认证失败…）
  esp_log_level_set("*", ESP_LOG_WARN);
  esp_log_level_set("BT_HCI", ESP_LOG_DEBUG);
  esp_log_level_set("BLE_HCI", ESP_LOG_DEBUG);    // NimBLE host HCI 解析（DLE 事件在此）
  esp_log_level_set("ble_hci", ESP_LOG_DEBUG);    // NimBLE HCI 传输（若存在）
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

  // 2) 音频帧出队 -> 串口。放这里而不是 GATT 回调里，理由见 handleAudio 注释。
  drainAudio();

  // 2.5) 语音键边界事件。PC 端据此界定"这一句"从哪里开始、到哪里结束。
  //      在音频帧之后发，保证同一轮里音频先落盘、边界标记后到，
  //      PC 端收到 VOICE 0 时，抬起之前的帧已经全部在缓冲区里了。
  {
    int8_t ve = g_voiceBtnEvent;
    if (ve >= 0) {
      g_voiceBtnEvent = -1;
      // 顺序说明：**先**把边界事件发给上位机，**再**下发开/关麦指令。
      //  - VOICE 0 先发，抬起前已在队列里的音频帧照样能出完（drainAudio 在本段之前）；
      //  - 关麦/开麦的写是阻塞调用（带响应写要等 ATT 确认），万一遥控器不认而
      //    卡到超时，也不能拖累切句标记的及时性。
      Serial.printf("# VOICE %d\n", (int)ve);
      if (g_voiceCtlAuto) voiceCtl((uint8_t)(ve ? 1 : 0));
    }
  }

  // 3) 板载灯：语音流 -> 红灯常亮；按键 -> 蓝灯闪；空闲 -> 灭
  //    起止标记也在这里打，回调上下文里一次 printf 都不做。
  if (g_voiceActive && millis() - g_lastVoiceMs > 300) {
    g_voiceActive = false;                     // 300ms 没有新帧 = 语音结束
  }
  if (g_voiceActive != g_voiceWasActive) {
    if (g_voiceActive) Serial.printf("# A START n=%lu\n", (unsigned long)g_audioFrames);
    else               Serial.println("# A END");
    g_voiceWasActive = g_voiceActive;
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
      g_voiceHeld   = false;      // 链路断了不会再有抬起事件，红灯必须清掉
      g_scanNow = true;
      Keyboard.releaseAll();
    } else {
      // 心跳：长时间没交互时主动读一次，避免链路被判定空闲
      if (millis() - g_lastAlive > HEARTBEAT_MS) {
        g_lastAlive = millis();
      }
      // 遥控器改参被接受之后补一次 ce_len（见 applyBigCeLen 注释）。
      // 800ms 的间隔是给它留出回应上一条请求的时间，避免请求叠在一起被拒。
      if (g_cePending && millis() - g_ceAppliedAt > 800) {
        g_cePending   = false;
        g_ceAppliedAt = millis();
        applyBigCeLen();
      }
    }
  }

  if (g_scanNow && millis() >= g_nextScanAt) {
    g_scanNow = false;
    scanOnce();
    Serial.printf("# t=%lu 扫描结束 target=%s\n", millis(), g_target != nullptr ? "有" : "无");
    if (g_holdConnectUntil != 0 && (int32_t)(millis() - g_holdConnectUntil) < 0) {
      // SEARCH 暂停期：照常扫描并把详单打出去，但**不连**。
      // 一旦在这里连上，遥控器立刻停止广播，上位机就再也抓不到它的 MAC 了。
      // 300ms 一轮，15 秒窗口里能扫十来轮，足够抓全。
      g_scanNow    = true;
      g_nextScanAt = millis() + 300;
    } else if (g_target == nullptr) {
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
