# Firmware tools

These tools give the agent a start for the firmware of a board: the functions of the
pins of a microcontroller, the devices on the I2C and SPI buses, the power domains,
and a device tree source. They are heuristic: they find a function from the name of
a net, and a device from the value of a symbol. Make sure that each result agrees
with the datasheet of the microcontroller (see [datasheet-lens.md](datasheet-lens.md)).

## Microcontrollers

The tools find a microcontroller by the Value field of its symbol (a case-insensitive
search):

| Family | Pattern | Examples |
|---|---|---|
| `stm32` | `STM32F`, `STM32H`, `STM32L` and a digit | STM32F407VGT6, STM32L432KC |
| `esp32` | `ESP32`, also with a suffix | ESP32, ESP32-S3 |
| `nrf52` | `nRF52` to `nRF59` | nRF52840, nRF5340, nRF54LM20B |
| `atmega` | `ATmega` and digits | ATmega328P, ATmega2560 |
| `samd` | `ATSAMD` and digits | ATSAMD21, ATSAMD51 |
| `rp2040` | `RP2040` | RP2040 |

Other microcontrollers, also other STM32 series (for example STM32G0, STM32U5, STM32C0),
are not found.

The pin names and the pin types come from the netlist of kicad-cli (the pins as
KiCad gives them, also the alternate function of a pin). For STM32, a pin name such
as `PA9` gives the port and the bit.

## Pin functions from net names

`analyze_pin_functions` and `extract_pinmux_config` give a function to a pin from the
name of its net:

| Function | Net names (examples) |
|---|---|
| I2C | `I2C1_SDA`, `SDA`, `SCL`, `TWI_SDA` |
| SPI | `SPI1_MOSI`, `MISO`, `SCK`, `CS`, `NSS` |
| UART | `UART2_TX`, `USART1_RX`, `TX`, `RX` |
| GPIO | `GPIO_5`, `IO_5`, `PA3`, `P1` |
| ADC | `ADC_1`, `AIN0`, `AN2` |
| PWM | `PWM_1`, `TIM2_CH1` |
| USB | `USB_DP`, `USB_DM`, `D+`, `D-` |
| Interrupt | `INT1`, `IRQ`, `ACCEL_INT` |

A net with no such name gets no function. A net name that matches by chance gets a
wrong function (for example a net `CS_SENSE` of a current sense amplifier). Use clear
net names in the schematic: then the result is better.

## Pin conflicts

`detect_pin_conflicts` uses the pin types of the netlist:

- Error: a net with more than one output or driver pin (output, power output, open
  collector, open emitter, tri-state). More power input pins on one net are correct.
- Warning: a net with only one pin.
- Information: a pin with no connection.

`run_erc` gives the full check of KiCad, with the pin conflict matrix of the project.

## Devices and buses

`extract_i2c_devices` and `extract_spi_devices` find the buses from the net names,
and the devices on them from the values of the symbols. The tools know the devices
of a small table:

| Group | Devices |
|---|---|
| Sensors | BMP280, BME280, BMP388, LSM6DS3, MPU6050, MPU9250, HTS221, LPS22HB, DHT22, SHT30, MAX31855 |
| Displays | ST7789, ST7735, ILI9341, SSD1306, SH1106 |
| Memory | AT24C256, W25Q128, GD25Q16 |
| Wireless | ESP8266, nRF24L01, SX1278 |
| USB to UART | CP2102, FT232RL, CH340G |
| Power | TP4056, AXP192, LP3985, AP2112 |

The I2C address of a device comes from the net name only (for example
`I2C_SDA_0x76`). With no address in the name, the device gets the address `0x00`.

`extract_power_domains` gives the power nets and the regulators. `extract_gpio_config`
gives the GPIO pins.

## Device tree sources

`generate_device_tree` makes a device tree source (`.dts`) for one of these targets:
`stm32f4` (default), `esp32`, `nrf52`. The source has the I2C and SPI nodes (for
`stm32f4` also the UART nodes), the devices of the table above, and the GPIO pins.

The source is a start, not a finished file:

- The unit addresses of the bus controllers are empty. Add them from the reference
  manual of the microcontroller or from the device tree include of the SoC.
- An I2C device with no address in its net name has the address `0x00`.
- The `compatible` strings come from the table of the tool. Make sure that they agree
  with the bindings of your kernel or RTOS (Linux, Zephyr).
- `validate_pin_configuration` gives the pin conflicts that the generation can see.
