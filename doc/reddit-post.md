# fw-pwrctl — shut up, fan (Framework Laptop 13, Linux)

My Framework 13 fan had one volume: annoying. Light browsing? Fan. Idle desktop? Believe it or not, also fan.

So I wrote a daemon to fix it.

## What it does

The Framework EC owns the fan and won't let you control it directly. Classic move. But it *does* base fan speed on board temperatures. So fw-pwrctl controls the CPU power limit (PL1) instead — lower power, cooler board, fan calms down on its own. The EC thinks everything is fine. Because it is.

It also bumps a DDR sensor threshold that's set way too low from the factory. That one fix alone cuts ~2,500 RPM at idle. You're welcome, eardrums.

## How it works

- Reads CPU temp every 2 seconds
- A PI controller adjusts PL1 (5–28W) to hold a target temp (default 75°C)
- Board cools down → EC slows the fan → silence

## Is it safe?

Three independent safety layers. Board VRM sensor guard. CPU critical override at 95°C. Sensor failure safe mode. And the EC's own 103°C hardware throttle is always there as a backstop, completely untouched. Your laptop will be fine. Probably quieter than it's ever been.

## What hardware?

- Framework Laptop 13, 12th Gen Intel (Alder Lake)
- i5-1240P tested, i7-1260P / i7-1280P should work
- Linux (Ubuntu 22.04+, kernel 6.x+), Python 3.10+, root

## Getting started

Three commands. Install ectool, run the install script, enable the service. Or try `--dry-run --debug` first to watch it think without touching anything.

GitHub: https://github.com/t3spe/fw-pwrctl

GPLv3. Questions, feedback, bug reports all welcome.
