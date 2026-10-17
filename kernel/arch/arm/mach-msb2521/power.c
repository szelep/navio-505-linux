// SPDX-License-Identifier: GPL-2.0-only
/* MSB2521 PMU: Power key and power off for the Navio 505.
 * Register evidence comes from the stock firmware, not a datasheet:
 *  PMU at 0xa0007e00: CE nk.exe 0x8030aa74, EBOOT 0.1104 0x83e12c08.
 *  +0x194 bit 1: ON/OFF key held (EBOOT key filter 0x83e0f890,
 *    OFFCharging 0x83e15908, CE OEMShutdown 0x8030a8ac).
 *  +0x184 bit 9: adapter present (CE 0x803080d4, EBOOT 0x83e14b08).
 * The stock keypad driver switches off after 1000 ms (g_u32ShutdownDelay).
 * Without an adapter CE cuts power in OEMShutdown 0x8030a7e8/0x803078ac.
 * With an adapter CE OEMPowerOff 0x8030ab24 resets the chip instead and
 * EBOOT charges in OFFCharging until Power is pressed or the adapter goes.
 */
#include <linux/delay.h>
#include <linux/init.h>
#include <linux/io.h>
#include <linux/jiffies.h>
#include <linux/kernel.h>
#include <linux/notifier.h>
#include <linux/of.h>
#include <linux/printk.h>
#include <linux/reboot.h>
#include <linux/workqueue.h>
#include <asm/proc-fns.h>

#define PMU_PHYS 0xa0007e00
#define RTC_PHYS 0xa0004800
#define CHIPTOP_PHYS 0xa0003c00
#define INTC_PHYS 0xa0005600

#define PMU_POWER_CUT 0x2c	/* low byte 0x5a: PMU switches off */
#define PMU_BOOT_SIGN 0x68	/* read by EBOOT; 0x00ff enters OFFCharging */
#define PMU_BOOT_AUX 0x6c
#define PMU_ADAPTER 0x184
#define PMU_KEY 0x194
#define PMU_WAKE_MASK0 0x1c0
#define PMU_WAKE_MASK1 0x1c4
#define PMU_STATUS_CLEAR0 0x1d0
#define PMU_STATUS_CLEAR1 0x1d4
#define RTC_KEY_FILTER 0x94	/* 0xaaaa: EBOOT wants Power held at power on */
#define CHIPTOP_RESET 0x18

#define KEY_POLL_MS 50
#define KEY_HOLD_MS 1000
#define KEY_RELEASE_WAIT_MS 10000

static void __iomem *pmu, *rtc, *chiptop, *power_intc;
static unsigned long pressed_since;
static bool key_armed, key_pressed;

static bool key_held(void)
{
	return readl(pmu + PMU_KEY) & BIT(1);
}

static int navio_power_off(struct sys_off_data *data)
{
	static const unsigned int intc_mask[] = { 0x00, 0x04, 0x30, 0x34 };
	unsigned int i, ms;

	/* Like CE: a key still held at the cut would wake the PMU again. */
	if (key_held())
		pr_emerg("navio505-power: release the Power key\n");
	for (ms = 0; key_held() && ms < KEY_RELEASE_WAIT_MS; ms += 10)
		mdelay(10);
	if (readw(pmu + PMU_ADAPTER) & BIT(9)) {
		pr_emerg("navio505-power: adapter present, restarting into EBOOT OFFCharging\n");
		writew(0x00ff, pmu + PMU_BOOT_SIGN);
		writel(0xffff, pmu + PMU_STATUS_CLEAR0);
		writew(0, pmu + PMU_BOOT_AUX);
		writew(0x00ff, chiptop + CHIPTOP_RESET);
		writew(0x0079, chiptop + CHIPTOP_RESET);
		mdelay(1000);
		pr_emerg("navio505-power: chip reset did not happen, cutting power\n");
	}
	for (i = 0; i < ARRAY_SIZE(intc_mask); i++)
		writew(0xffff, power_intc + intc_mask[i]);
	writel(0xedff, pmu + PMU_WAKE_MASK0);
	writel(0xffff, pmu + PMU_WAKE_MASK1);
	writel(0xffff, pmu + PMU_STATUS_CLEAR0);
	writew(0x0ff0, pmu + PMU_BOOT_SIGN);
	writew(0, rtc + RTC_KEY_FILTER);
	mdelay(10);
	writew(0xffff, pmu + PMU_STATUS_CLEAR0);
	writew(0xffff, pmu + PMU_STATUS_CLEAR1);
	writel((readl(pmu + PMU_POWER_CUT) & ~0xff) | 0x5a, pmu + PMU_POWER_CUT);
	mdelay(3000);
	pr_emerg("navio505-power: PMU did not cut power, system halted\n");
	for (;;)
		cpu_do_idle();
	return NOTIFY_DONE;
}

static void key_poll(struct work_struct *work);
static DECLARE_DELAYED_WORK(key_work, key_poll);

static void key_poll(struct work_struct *work)
{
	bool held = key_held();

	if (!key_armed) {
		/* EBOOT makes the user hold Power to boot; ignore that press. */
		key_armed = !held;
	} else if (!held) {
		if (key_pressed)
			pr_info("navio505-power: Power released after %u ms, ignored\n",
				jiffies_to_msecs(jiffies - pressed_since));
		key_pressed = false;
	} else if (!key_pressed) {
		key_pressed = true;
		pressed_since = jiffies;
		pr_notice("navio505-power: hold Power for 1 s to switch off\n");
	} else if (time_after_eq(jiffies, pressed_since + msecs_to_jiffies(KEY_HOLD_MS))) {
		pr_notice("navio505-power: Power held, switching off\n");
		kernel_power_off();
		return;
	}
	schedule_delayed_work(&key_work, msecs_to_jiffies(KEY_POLL_MS));
}

static int __init navio_power_init(void)
{
	struct sys_off_handler *handler;

	if (!of_machine_is_compatible("goclever,navio505"))
		return 0;
	pmu = ioremap(PMU_PHYS, 0x200);
	rtc = ioremap(RTC_PHYS, 0x100);
	chiptop = ioremap(CHIPTOP_PHYS, 0x100);
	power_intc = ioremap(INTC_PHYS, 0x100);
	if (!pmu || !rtc || !chiptop || !power_intc) {
		pr_err("navio505-power: cannot map PMU registers\n");
		return -ENOMEM;
	}
	handler = register_sys_off_handler(SYS_OFF_MODE_POWER_OFF, SYS_OFF_PRIO_PLATFORM,
					   navio_power_off, NULL);
	if (IS_ERR(handler))
		return PTR_ERR(handler);
	pr_info("navio505-power: key=%u adapter=%u sign=%04x aux=%04x filter=%04x wake=%04x:%04x status=%04x:%04x\n",
		key_held(), !!(readw(pmu + PMU_ADAPTER) & BIT(9)),
		readw(pmu + PMU_BOOT_SIGN), readw(pmu + PMU_BOOT_AUX),
		readw(rtc + RTC_KEY_FILTER), readw(pmu + PMU_WAKE_MASK0),
		readw(pmu + PMU_WAKE_MASK1), readw(pmu + 0x1e0), readw(pmu + 0x1e4));
	schedule_delayed_work(&key_work, 0);
	return 0;
}
device_initcall(navio_power_init);
