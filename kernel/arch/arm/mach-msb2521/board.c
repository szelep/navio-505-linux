// SPDX-License-Identifier: GPL-2.0-only
/* GoClever Navio 505 / MStar MSB2521 U03 board support.
 * Register evidence: stock CE nk.exe OALIntrInit 0x80308f58,
 * OEMInterruptHandler 0x80309520, OALTimerInit 0x80309dec,
 * EBOOT UART init 0x83e1327c and timer init 0x83e0e050.
 * These are NOT the Cortex-A7/GIC registers of ARCH_MSTARV7.
 */
#include <linux/bitops.h>
#include <linux/clockchips.h>
#include <linux/clocksource.h>
#include <linux/delay.h>
#include <linux/interrupt.h>
#include <linux/io.h>
#include <linux/irq.h>
#include <linux/irqdomain.h>
#include <linux/of.h>
#include <linux/of_address.h>
#include <linux/of_platform.h>
#include <linux/sched_clock.h>
#include <linux/slab.h>
#include <asm/exception.h>
#include <asm/mach/arch.h>
#include <asm/mach/map.h>

#define IRQ_PHYS 0xa0005600
#define TIMER_EVENT_PHYS 0xa00069c0
#define TIMER_SOURCE_PHYS 0xa00077c0
#define CLK_PHYS 0xa0003c00
#define TIMER_ENABLE BIT(0)
#define TIMER_IRQ_ENABLE BIT(8)

static void __iomem *intc;
static struct irq_domain *domain;
static void __iomem *event_base, *source_base;
static unsigned long timer_rate;
static const unsigned int mask_reg[] = {0x00, 0x04, 0x30, 0x34};

static void msb_mask(struct irq_data *d)
{
	unsigned int reg = mask_reg[d->hwirq / 16];
	writew(readw(intc + reg) | BIT(d->hwirq % 16), intc + reg);
}

static void msb_unmask(struct irq_data *d)
{
	unsigned int reg = mask_reg[d->hwirq / 16];
	writew(readw(intc + reg) & ~BIT(d->hwirq % 16), intc + reg);
}

static void msb_ack(struct irq_data *d)
{
	/* Legacy FIQ bank 0..31 is dispatched on the CPU IRQ path in CE.
	 * Its pending latch is W1C; never read/OR other pending bits.
	 * IRQ bank 32..63 is level-sensitive and has no such latch clear.
	 */
	if (d->hwirq < 32)
		writew(BIT(d->hwirq % 16), intc + 0x10 + (d->hwirq / 16) * 4);
}

static struct irq_chip msb_chip = {
	.name = "msb2521",
	.irq_mask = msb_mask,
	.irq_unmask = msb_unmask,
	.irq_ack = msb_ack,
};

static int msb_irq_map(struct irq_domain *d, unsigned int irq, irq_hw_number_t hwirq)
{
	irq_set_chip_and_handler(irq, &msb_chip, handle_level_irq);
	irq_set_probe(irq);
	return 0;
}

static const struct irq_domain_ops msb_irq_ops = {
	.map = msb_irq_map,
	.xlate = irq_domain_xlate_onecell,
};

static void __exception_irq_entry msb_handle_irq(struct pt_regs *regs)
{
	unsigned int irq;
	/* Vendor handler dispatches one vector per CPU entry. A vector may
	 * still show the previous source while its clear propagates. */
	irq = readb(intc + 0x88);
	if (irq == 0xff)
		irq = readb(intc + 0x8c);
	if (irq >= 64)
		return;
	if (generic_handle_domain_irq(domain, irq)) {
		unsigned int reg = mask_reg[irq / 16];
		writew(readw(intc + reg) | BIT(irq % 16), intc + reg);
	}
}

static void __init msb_init_irq(void)
{
	struct device_node *node;
	unsigned int i;
	intc = ioremap(IRQ_PHYS, 0x100);
	if (!intc)
		panic("MSB2521: cannot map IRQ registers");
	for (i = 0; i < ARRAY_SIZE(mask_reg); i++)
		writew(0xffff, intc + mask_reg[i]);
	writew(0xffff, intc + 0x10);
	writew(0xffff, intc + 0x14);
	node = of_find_compatible_node(NULL, NULL, "mstar,msb2521-intc");
	if (!node)
		panic("MSB2521: missing IRQ node");
	domain = irq_domain_add_linear(node, 64, &msb_irq_ops, NULL);
	of_node_put(node);
	if (!domain)
		panic("MSB2521: cannot create IRQ domain");
	set_handle_irq(msb_handle_irq);
	pr_info("MSB2521 legacy IRQ controller at %08x\n", IRQ_PHYS);
}

static u32 msb_counter(void)
{
	u32 lo, hi;
	/* CAP is latched by the low-word read: datasheet requires LSB first.
	 * Match the 32-bit bus accesses used by the shipping EBOOT/CE timer
	 * code; only the low 16 bits in each RIU word belong to the counter.
	 */
	lo = readl(source_base + 0x10) & 0xffff;
	hi = readl(source_base + 0x14) & 0xffff;
	return (hi << 16) | lo;
}

static u64 msb_clock_read(struct clocksource *cs)
{
	return msb_counter();
}

static u64 notrace msb_sched_clock(void)
{
	return msb_counter();
}

static struct clocksource msb_clock = {
	.name = "msb2521-timer2", .rating = 250,
	.read = msb_clock_read, .mask = CLOCKSOURCE_MASK(32),
	.flags = CLOCK_SOURCE_IS_CONTINUOUS,
};

static int msb_shutdown(struct clock_event_device *evt)
{
	writel(0, event_base);
	return 0;
}

static int msb_periodic(struct clock_event_device *evt)
{
	u32 ticks = DIV_ROUND_CLOSEST(timer_rate, HZ) - 1;
	writel(0, event_base);
	writel(ticks & 0xffff, event_base + 0x08);
	writel(ticks >> 16, event_base + 0x0c);
	writel(TIMER_ENABLE | TIMER_IRQ_ENABLE, event_base);
	return 0;
}

static struct clock_event_device msb_event = {
	.name = "msb2521-timer0", .rating = 250,
	.features = CLOCK_EVT_FEAT_PERIODIC,
	.set_state_shutdown = msb_shutdown,
	.set_state_periodic = msb_periodic,
	.tick_resume = msb_shutdown,
};

static irqreturn_t msb_timer_irq(int irq, void *dev)
{
	struct clock_event_device *evt = dev;
	/* Ignore stale controller vectors: only a hardware TIMER_HIT is a tick.
	 * Restart the device first, then clear any controller latch reasserted
	 * between the generic IRQ ack and deassertion of the timer source. */
	if (!(readl(event_base + 0x04) & BIT(0)))
		return IRQ_HANDLED;
	writel(0, event_base);
	writel(TIMER_ENABLE | TIMER_IRQ_ENABLE, event_base);
	writel(BIT(1), intc + 0x10);

	evt->event_handler(evt);
	return IRQ_HANDLED;
}

static void __init msb_init_time(void)
{
	void __iomem *clk = ioremap(CLK_PHYS, 0x100);
	unsigned int irq;
	u16 strap;
	if (!clk)
		panic("MSB2521: cannot map clock strap");
	strap = readw(clk + 0x2c);
	iounmap(clk);
	timer_rate = (strap & BIT(6)) ? 24000000 : 16369000;
	source_base = ioremap(TIMER_SOURCE_PHYS, 0x20);
	event_base = ioremap(TIMER_EVENT_PHYS, 0x20);
	if (!source_base || !event_base)
		panic("MSB2521: cannot map timers");
	writel(0, source_base);
	writel(0xffff, source_base + 0x08);
	writel(0xffff, source_base + 0x0c);
	writel(TIMER_ENABLE, source_base);
	if (clocksource_register_hz(&msb_clock, timer_rate))
		panic("MSB2521: clocksource registration failed");
	sched_clock_register(msb_sched_clock, 32, timer_rate);
	writel(0, event_base);
	irq = irq_create_mapping(domain, 1);
	if (!irq || request_irq(irq, msb_timer_irq, IRQF_TIMER, "msb2521-timer0", &msb_event))
		panic("MSB2521: cannot register timer0 IRQ1");
	msb_event.irq = irq;
	msb_event.cpumask = cpumask_of(0);
	clockevents_config_and_register(&msb_event, timer_rate, 3, 0xffffffff);
	pr_info("MSB2521 timer/UART clock %lu Hz, strap=%04x, timer IRQ=%u\n",
		timer_rate, strap, irq);
}

static void __init msb_init_machine(void)
{
	struct device_node *uart;
	struct property *clock;
	uart = of_find_compatible_node(NULL, NULL, "ns16550a");
	if (uart) {
		/* Keep the exact vendor-selected clock; do not reprogram any PLL. */
		clock = kzalloc(sizeof(*clock), GFP_KERNEL);
		if (!clock)
			panic("MSB2521: UART clock allocation failed");
		clock->name = kstrdup("clock-frequency", GFP_KERNEL);
		clock->length = sizeof(__be32);
		clock->value = kmalloc(clock->length, GFP_KERNEL);
		if (!clock->name || !clock->value)
			panic("MSB2521: UART clock property allocation failed");
		*(__be32 *)clock->value = cpu_to_be32(timer_rate);
		if (of_update_property(uart, clock))
			panic("MSB2521: UART clock update failed");
		of_node_put(uart);
	}
	of_platform_default_populate(NULL, NULL, NULL);
	/* RAM bring-up evidence: raw counter progression, independent of
	 * Linux timekeeping. Delay calibration is only a preset estimate. */
	for (int sample = 0; sample < 5; sample++) {
		u32 cap = msb_counter();
		pr_info("MSB2521 sample=%d t2=%08x t0ctrl=%08x max=%08x:%08x cap=%08x:%08x hit=%08x irqvec=%02x pending=%04x\n",
			sample, cap, readl(event_base),
			readl(event_base + 0x0c), readl(event_base + 0x08),
			readl(event_base + 0x14), readl(event_base + 0x10),
			readl(event_base + 0x04), readb(intc + 0x88), readw(intc + 0x18));
		mdelay(100);
	}

}

static const char *const msb_compat[] __initconst = { "goclever,navio505", NULL };
DT_MACHINE_START(MSB2521_DT, "GoClever Navio 505 / MStar MSB2521 U03")
	.dt_compat = msb_compat,
	.map_io = debug_ll_io_init,
	.init_irq = msb_init_irq,
	.init_time = msb_init_time,
	.init_machine = msb_init_machine,
MACHINE_END
