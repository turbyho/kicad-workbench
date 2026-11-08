"""Device tree generation tools for KiCad Workbench.

This module provides device tree source (.dts) generation capabilities including:
- GPIO pin configuration extraction
- I2C/SPI/UART device detection
- Power domain analysis
- SOC-specific device tree generation
"""

import asyncio
from pathlib import Path

from jinja2 import Template

from ..parsers.kicad_fields import with_field_rules_note
from ..server import mcp
from ..utils.file_save import AfterSaveError
from ..utils.file_versions import with_schematic_versions
from ..utils.kicad_text import write_kicad_text
from ..utils.tool_errors import error_answer
from .schematic import BaseNames, components_of_project

# Device tree compatible strings database
DEVICE_TREE_BINDINGS = {
    "Sensor": {
        "BMP280": {"compatible": "bosch,bmp280"},
        "BME280": {"compatible": "bosch,bme280"},
        "BMP388": {"compatible": "bosch,bmp388"},
        "LSM6DS3": {"compatible": "st,lsm6ds3"},
        "MPU6050": {"compatible": "invensense,mpu6050"},
        "MPU9250": {"compatible": "invensense,mpu9250"},
        "HTS221": {"compatible": "st,hts221"},
        "LPS22HB": {"compatible": "st,lps22hb"},
        "DHT22": {"compatible": "dht22"},
        "SHT30": {"compatible": "sensirion,sht3x"},
        "MAX31855": {"compatible": "maxim,max31855"},
    },
    "Display": {
        "ST7789": {"compatible": "sitronix,st7789"},
        "ST7735": {"compatible": "sitronix,st7735"},
        "ILI9341": {"compatible": "ilitek,ili9341"},
        "SSD1306": {"compatible": "solomon,ssd1306"},
        "SH1106": {"compatible": "sinowealth,sh1106"},
    },
    "Memory": {
        "AT24C256": {"compatible": "atmel,24c256"},
        "W25Q128": {"compatible": "jedec,spi-nor"},
        "GD25Q16": {"compatible": "jedec,spi-nor"},
    },
    "Wireless": {
        "ESP8266": {"compatible": "espressif,esp8266"},
        "nRF24L01": {"compatible": "nordic,nrf24l01"},
        "SX1278": {"compatible": "semtech,sx1278"},
    },
    "Connectivity": {
        "CP2102": {"compatible": "silabs,cp2102"},
        "FT232RL": {"compatible": "ftdi,ft232rl"},
        "CH340G": {"compatible": "wch,ch340"},
    },
    "Power": {
        "TP4056": {"compatible": "tp4056"},
        "AXP192": {"compatible": "x-powers,axp192"},
        "LP3985": {"compatible": "ti,lp3985"},
        "AP2112": {"compatible": "tps,ap2112"},
    },
}


# SOC-specific device tree templates
DEVICE_TREE_TEMPLATES = {
    "stm32f4": """/* Device Tree for STM32F4 SOC */
/* Auto-generated from KiCad schematic */

#include <dt-bindings/gpio/gpio.h>
#include <dt-bindings/pinctrl/stm32-pinctrl.h>

/ {
  model = "{{ board_name }}";
  compatible = "st,stm32f4";

  /* GPIO Configuration */
  {% for pin in gpio_pins %}
  gpio_{{ pin.name }}: gpio@{{ pin.number }} {
    gpio-controller;
    #gpio-cells = <2>;
    status = "okay";
  };
  {% endfor %}

  /* I2C Devices */
  {% for bus in i2c_buses %}
  i2c{{ bus.number }}: i2c@{{ bus.address }} {
    compatible = "st,stm32-i2c";
    #address-cells = <1>;
    #size-cells = <0>;
    status = "okay";

    {% for device in bus.devices %}
    {{ device.name }}: {{ device.compatible }}@{{ device.address }} {
      compatible = "{{ device.compatible }}";
      reg = <{{ device.address }}>;
      {% if device.properties %}
      {% for key, value in device.properties.items() %}
      {{ key }} = <{{ value }}>;
      {% endfor %}
      {% endif %}
    };
    {% endfor %}
  };

  {% endfor %}

  /* SPI Devices */
  {% for bus in spi_buses %}
  spi{{ bus.number }}: spi@{{ bus.address }} {
    compatible = "st,stm32-spi";
    #address-cells = <1>;
    #size-cells = <0>;
    status = "okay";

    {% for device in bus.devices %}
    {{ device.name }}: {{ device.compatible }}@{{ device.cs }} {
      compatible = "{{ device.compatible }}";
      reg = <{{ device.cs }}>;
      spi-max-frequency = <{{ device.frequency }}>;
      {% if device.properties %}
      {% for key, value in device.properties.items() %}
      {{ key }} = <{{ value }}>;
      {% endfor %}
      {% endif %}
    };
    {% endfor %}
  };

  {% endfor %}

  /* UART Devices */
  {% for uart in uarts %}
  usart{{ uart.number }}: serial@{{ uart.address }} {
    compatible = "st,stm32-usart";
    status = "okay";
  };
  {% endfor %}
};
""",
    "esp32": """/* Device Tree for ESP32 SOC */
/* Auto-generated from KiCad schematic */

#include <dt-bindings/gpio/gpio.h>

/ {
  model = "{{ board_name }}";
  compatible = "espressif,esp32";

  /* GPIO Configuration */
  {% for pin in gpio_pins %}
  gpio_{{ pin.number }}: gpio@{{ pin.number }} {
    gpio-controller;
    #gpio-cells = <2>;
    status = "okay";
  };
  {% endfor %}

  /* I2C Devices */
  {% for bus in i2c_buses %}
  i2c{{ bus.number }}: i2c@{{ bus.address }} {
    compatible = "espressif,esp32-i2c";
    #address-cells = <1>;
    #size-cells = <0>;
    status = "okay";

    {% for device in bus.devices %}
    {{ device.name }}: {{ device.compatible }}@{{ device.address }} {
      compatible = "{{ device.compatible }}";
      reg = <{{ device.address }}>;
      {% if device.properties %}
      {% for key, value in device.properties.items() %}
      {{ key }} = <{{ value }}>;
      {% endfor %}
      {% endif %}
    };
    {% endfor %}
  };

  {% endfor %}

  /* SPI Devices */
  {% for bus in spi_buses %}
  spi{{ bus.number }}: spi@{{ bus.address }} {
    compatible = "espressif,esp32-spi";
    #address-cells = <1>;
    #size-cells = <0>;
    status = "okay";

    {% for device in bus.devices %}
    {{ device.name }}: {{ device.compatible }}@{{ device.cs }} {
      compatible = "{{ device.compatible }}";
      reg = <{{ device.cs }}>;
      spi-max-frequency = <{{ device.frequency }}>;
      {% if device.properties %}
      {% for key, value in device.properties.items() %}
      {{ key }} = <{{ value }}>;
      {% endfor %}
      {% endif %}
    };
    {% endfor %}
  };

  {% endfor %}
};
""",
    "nrf52": """/* Device Tree for nRF52 SOC */
/* Auto-generated from KiCad schematic */

#include <dt-bindings/gpio/gpio.h>

/ {
  model = "{{ board_name }}";
  compatible = "nordic,nrf52";

  /* GPIO Configuration */
  {% for pin in gpio_pins %}
  gpio_{{ pin.number }}: gpio@{{ pin.number }} {
    gpio-controller;
    #gpio-cells = <2>;
    status = "okay";
  };
  {% endfor %}

  /* I2C Devices */
  {% for bus in i2c_buses %}
  i2c{{ bus.number }}: i2c@{{ bus.address }} {
    compatible = "nordic,nrf52-i2c";
    #address-cells = <1>;
    #size-cells = <0>;
    status = "okay";

    {% for device in bus.devices %}
    {{ device.name }}: {{ device.compatible }}@{{ device.address }} {
      compatible = "{{ device.compatible }}";
      reg = <{{ device.address }}>;
      {% if device.properties %}
      {% for key, value in device.properties.items() %}
      {{ key }} = <{{ value }}>;
      {% endfor %}
      {% endif %}
    };
    {% endfor %}
  };

  {% endfor %}

  /* SPI Devices */
  {% for bus in spi_buses %}
  spi{{ bus.number }}: spi@{{ bus.address }} {
    compatible = "nordic,nrf52-spi";
    #address-cells = <1>;
    #size-cells = <0>;
    status = "okay";

    {% for device in bus.devices %}
    {{ device.name }}: {{ device.compatible }}@{{ device.cs }} {
      compatible = "{{ device.compatible }}";
      reg = <{{ device.cs }}>;
      spi-max-frequency = <{{ device.frequency }}>;
      {% if device.properties %}
      {% for key, value in device.properties.items() %}
      {{ key }} = <{{ value }}>;
      {% endfor %}
      {% endif %}
    };
    {% endfor %}
  };

  {% endfor %}
};
""",
}


def _find_component_binding(component_value: str, component_type: str) -> dict | None:
    """Find device tree binding for a component.

    Args:
        component_value: Component value/part number
        component_type: Component category (Sensor, Display, etc.)

    Returns:
        Device tree binding dict or None if not found
    """
    if not component_value or not component_type:
        return None

    component_value_upper = component_value.upper()

    # Try exact match first
    if component_type in DEVICE_TREE_BINDINGS:
        if component_value_upper in DEVICE_TREE_BINDINGS[component_type]:
            return DEVICE_TREE_BINDINGS[component_type][component_value_upper]

        # Try partial match
        for key, value in DEVICE_TREE_BINDINGS[component_type].items():
            if key in component_value_upper or component_value_upper in key:
                return value

    return None


def _infer_peripheral_type(net_name: str) -> str | None:
    """Infer peripheral type from net name.

    Args:
        net_name: Net name to analyze

    Returns:
        Peripheral type (I2C, SPI, UART, GPIO) or None
    """
    if not net_name:
        return None

    net_name_upper = net_name.upper()

    if "I2C" in net_name_upper or "TWI" in net_name_upper:
        return "I2C"
    elif "SPI" in net_name_upper:
        return "SPI"
    elif "UART" in net_name_upper or "USART" in net_name_upper or "SERIAL" in net_name_upper:
        return "UART"
    elif "GPIO" in net_name_upper or "IO" in net_name_upper:
        return "GPIO"

    return None


def _extract_i2c_address_from_net(net_name: str) -> int | None:
    """Extract I2C address from net name.

    Args:
        net_name: Net name (e.g., I2C_SDA_0x76)

    Returns:
        I2C address as integer or None
    """
    import re

    # Look for hex address pattern
    match = re.search(r"0x[0-9A-Fa-f]+", net_name)
    if match:
        return int(match.group(0), 16)

    # Look for decimal address pattern
    match = re.search(r"[_\s](\d{2,3})$", net_name)
    if match:
        address = int(match.group(1))
        if 1 <= address <= 127:  # Valid I2C address range
            return address

    return None


@mcp.tool()
@with_schematic_versions("schematic_path")
@with_field_rules_note
async def generate_device_tree(
    schematic_path: str,
    target_soc: str = "stm32f4",
    output_path: str = "",
) -> str:
    """Make a device tree source (.dts) from a schematic (heuristic).

    The tool finds the buses and the devices on them from the values and the net
    names. It does not check the pin conflicts (validate_pin_configuration). Do a
    review of the result before you use it. See docs/firmware.md of the repository
    for the limits.

    Args:
        schematic_path: A .kicad_sch file of the project. The tool uses the
            components and the netlist of the full project.
        target_soc: The family of the microcontroller: "stm32f4" (default),
            "esp32" or "nrf52".
        output_path: Optional .dts file to write. Empty: the tool writes no file.

    Returns:
        The device tree source, or an error.
    """
    try:
        sch_path = Path(schematic_path)
        if not sch_path.exists():
            return f"❌ **Schematic file not found:** {schematic_path}"

        # Validate SOC family
        if target_soc not in DEVICE_TREE_TEMPLATES:
            return f"""❌ **Unsupported SOC Family**

**Requested:** {target_soc}
**Supported:** {', '.join(DEVICE_TREE_TEMPLATES.keys())}

Please specify a supported SOC family."""

        from .netlist import current_netlist

        # The netlist that kicad-cli writes now, then the components of the project with
        # the KiCad of the netlist (components_of_project: the pins of each unit).
        netlist_parser = await current_netlist(sch_path)

        # Extract device tree data
        dt_data = {
            "board_name": sch_path.stem,
            "gpio_pins": [],
            "i2c_buses": [],
            "spi_buses": [],
            "uarts": [],
        }

        # Analyze components and extract peripheral configuration
        components = await components_of_project(sch_path, netlist_parser.kicad_version)

        # The components whose nodes the device tree uses (for the warning).
        used: dict[str, None] = {}
        for component in components:
            comp_ref = component.reference
            comp_value = component.value
            # Find device tree binding
            for category, _bindings in DEVICE_TREE_BINDINGS.items():
                binding = _find_component_binding(comp_value, category)
                if binding:
                    # The net of each node of the component in the netlist (a pin
                    # number can be on more than one net).
                    nl_comp = netlist_parser.get_components().get(comp_ref)
                    connections = [
                        {
                            "net": node.net,
                            "peripheral": _infer_peripheral_type(node.net),
                            "pin": node.number,
                        }
                        for node in (nl_comp.nodes if nl_comp else [])
                        if node.net
                    ]

                    if connections:
                        # Determine peripheral type based on connections
                        i2c_nets = [c for c in connections if c["peripheral"] == "I2C"]
                        spi_nets = [c for c in connections if c["peripheral"] == "SPI"]
                        uart_nets = [c for c in connections if c["peripheral"] == "UART"]
                        if i2c_nets or spi_nets or uart_nets:
                            used[comp_ref] = None  # The device tree gets a node of it.

                        if i2c_nets:
                            # I2C device
                            i2c_address = _extract_i2c_address_from_net(i2c_nets[0]["net"])

                            device = {
                                "name": comp_ref,
                                "compatible": binding["compatible"],
                                "address": i2c_address or 0x00,
                                "properties": {},
                            }

                            # Add to I2C bus. Bus MMIO base is SoC-specific; left
                            # empty instead of wrongly stamping an STM32 address on
                            # every target (esp32, nrf52, ...).
                            dt_data["i2c_buses"].append({
                                "number": 1,  # Default to I2C1
                                "address": "",
                                "devices": [device],
                            })

                        elif spi_nets:
                            # SPI device
                            device = {
                                "name": comp_ref,
                                "compatible": binding["compatible"],
                                "cs": 0,  # Default CS
                                "frequency": 1000000,  # 1 MHz default
                                "properties": {},
                            }

                            # Add to SPI bus (address left empty — SoC-specific)
                            dt_data["spi_buses"].append({
                                "number": 1,  # Default to SPI1
                                "address": "",
                                "devices": [device],
                            })

                        elif uart_nets:
                            # UART device (address left empty — SoC-specific)
                            dt_data["uarts"].append({
                                "number": 1,  # Default to USART1
                                "address": "",
                            })

        # Extract GPIO pins
        for component in components:
            comp_ref = component.reference
            comp_value = component.value

            # Check if this is an MCU
            if any(soc in comp_value.upper() for soc in ["STM32", "ESP32", "NRF"]):
                # Extract GPIO pins from netlist (iterate the MCU's real pins)
                # Each pin number one time: a number can be on more than one net.
                nl_comp = netlist_parser.get_components().get(comp_ref)
                numbers = dict.fromkeys(
                    node.number
                    for node in (nl_comp.nodes if nl_comp else [])
                    if "GPIO" in node.net.upper() or "IO" in node.net.upper()
                )
                dt_data["gpio_pins"] += [
                    {"number": number, "name": f"P{number}"} for number in numbers
                ]
                if numbers:
                    used[comp_ref] = None

        # Generate device tree from template
        template = Template(DEVICE_TREE_TEMPLATES[target_soc])
        device_tree = template.render(**dt_data)
        # A pin number on more than one net: the result can be wrong, thus no check mark.
        note = netlist_parser.pins_on_more_nets_note(used)
        sign = "⚠️" if note else "✅"
        note = f"\n\n{note}" if note else ""

        # Save to file if output path specified
        if output_path:
            output = Path(output_path)
            output.parent.mkdir(parents=True, exist_ok=True)

            # UTF-8 on each platform, not the locale encoding (on Windows the code
            # page of the region). write_kicad_text encodes the text before the file
            # opens, thus an encode error keeps the old file. Off the event loop: the
            # save flushes the file to the disk.
            saved = ""
            try:
                await asyncio.to_thread(write_kicad_text, output, device_tree)
            except AfterSaveError as e:  # the file has the device tree
                saved = f"\n\n⚠️ {e}"

            return f"""{sign} **Device Tree Generated Successfully**

**Schematic:** {schematic_path}
**Target SOC:** {target_soc}
**Output:** {output_path}

## Device Tree Contents

- **GPIO Pins:** {len(dt_data['gpio_pins'])}
- **I2C Buses:** {len(dt_data['i2c_buses'])}
- **SPI Buses:** {len(dt_data['spi_buses'])}
- **UARTs:** {len(dt_data['uarts'])}

The device tree source file has been generated and saved to the specified location.

## Next Steps

1. Review and customize the generated device tree
2. Compile with device tree compiler: `dtc -I dts -O dtb -o output.dtb {output_path}`
3. Test on target hardware
4. Iterate as needed{saved}{note}"""

        else:
            # Return device tree content directly
            return f"""{sign} **Device Tree Generated Successfully**

**Schematic:** {schematic_path}
**Target SOC:** {target_soc}

## Device Tree Contents

- **GPIO Pins:** {len(dt_data['gpio_pins'])}
- **I2C Buses:** {len(dt_data['i2c_buses'])}
- **SPI Buses:** {len(dt_data['spi_buses'])}
- **UARTs:** {len(dt_data['uarts'])}

## Generated Device Tree

```dts
{device_tree}
```

## Next Steps

1. Review and customize the generated device tree
2. Save to file: `{sch_path.stem}.dts`
3. Compile with device tree compiler: `dtc -I dts -O dtb -o output.dtb`
4. Test on target hardware{note}"""

    except Exception as e:
        return error_answer(e, f"Device tree generation for {schematic_path} failed")


@mcp.tool()
@with_schematic_versions("schematic_path")
@with_field_rules_note
async def extract_gpio_config(
    schematic_path: str,
    soc_family: str = "",
) -> str:
    """Give the GPIO pins of the microcontrollers of a schematic (heuristic).

    A microcontroller is a component with STM32, ESP32, NRF or ATMEGA in its value.
    A GPIO pin is a pin on a net with "GPIO" or "IO" in its name (also, for example,
    RADIO). For STM32 the tool gives the port and the bit of each pin.

    Args:
        schematic_path: A .kicad_sch file of the project. The tool uses the
            components and the netlist of the full project.
        soc_family: Optional text in the value of the microcontroller, for
            example "stm32" (no difference of case). Empty: each microcontroller.

    Returns:
        A table of the GPIO pins of each microcontroller.
    """
    try:
        sch_path = Path(schematic_path)
        if not sch_path.exists():
            return f"❌ **Schematic file not found:** {schematic_path}"

        # Obtain netlist
        from .netlist import current_netlist
        from .pin_analysis import STM32_PIN

        netlist_parser = await current_netlist(sch_path)

        # The name of the library pin (base_name): the GPIO port comes from it, not from
        # an alternate function (SchematicParser._component_pins).
        base_names = {
            c.reference: BaseNames(c)
            for c in await components_of_project(sch_path, netlist_parser.kicad_version)
        }
        no_names = BaseNames(None)  # A component that the schematic model does not have.

        # Extract GPIO configurations: one for each pin (component, number and name),
        # with all its GPIO nets (a pin number can be on more than one net).
        configs_of: dict[tuple[str, str, str], dict] = {}

        # NetlistParser.get_components() returns dict[ref, NetlistComponent];
        # there is no get_all_components() (previous call always crashed).
        for comp_ref, comp in netlist_parser.get_components().items():
            comp_value = comp.value

            # Filter by SOC family if specified
            if soc_family and soc_family.lower() not in comp_value.lower():
                continue

            # Check if this is an MCU
            if any(soc in comp_value.upper() for soc in ["STM32", "ESP32", "NRF", "ATMEGA"]):
                for node in comp.nodes:
                    if "GPIO" in node.net.upper() or "IO" in node.net.upper():
                        pin_name = base_names.get(comp_ref, no_names).of(node) or node.number
                        config = configs_of.setdefault(
                            (comp_ref, node.number, pin_name),
                            {
                                "component": comp_ref,
                                "pin_number": node.number,
                                "pin_name": pin_name,
                                "nets": [],
                                "soc": comp_value,
                            },
                        )
                        if node.net not in config["nets"]:
                            config["nets"].append(node.net)
        gpio_configs = list(configs_of.values())
        for config in gpio_configs:
            config["net"] = ", ".join(config.pop("nets"))

        if not gpio_configs:
            return f"""⚠️ **No GPIO Configurations Found**

**Schematic:** {schematic_path}
{'**SOC Family Filter:** ' + soc_family if soc_family else ''}

No GPIO configurations were found in the schematic.

**Next Steps:**
1. Ensure schematic has MCU components
2. Check net names contain 'GPIO' or 'IO'
3. Try without SOC family filter"""

        # Format results
        result = f"""# GPIO Configuration Extraction

**Schematic:** {schematic_path}
{'**SOC Family:** ' + soc_family if soc_family else ''}
**Total GPIO Pins:** {len(gpio_configs)}

## GPIO Pin Details

| Component | Pin | Net | SOC |
|-----------|-----|-----|-----|
"""

        for config in gpio_configs:
            result += f"| {config['component']} | {config['pin_name']} ({config['pin_number']}) | {config['net']} | {config['soc']} |\n"

        # Add code generation suggestions
        result += "\n## Code Generation Suggestions\n\n"

        # Group by SOC
        soc_groups = {}
        for config in gpio_configs:
            soc = config["soc"]
            if soc not in soc_groups:
                soc_groups[soc] = []
            soc_groups[soc].append(config)

        for soc, configs in soc_groups.items():
            result += f"### {soc}\n\n```c\n"
            result += f"// GPIO configuration for {soc}\n\n"
            # Each C variable and each call one time: the numbers of a stacked pin
            # ("[5-6]") have one pin name.
            done: set[str] = set()

            for config in configs[:10]:  # Show first 10
                result += f"// {config['pin_name']} ({config['pin_number']}): {config['net']}\n"
                # The port and the bit of the pin come from its name "PXn" (PA7: GPIOA,
                # GPIO_PIN_7), not from the number of the package pin.
                stm32_pin = STM32_PIN.match(config["pin_name"])
                if "STM32" in soc.upper() and stm32_pin:
                    port, bit = stm32_pin.group(1).upper(), stm32_pin.group(2)
                    var = f"gpio_p{port.lower()}{bit}"  # A C name also for "PA13/SWDIO".
                    if var not in done:
                        done.add(var)
                        result += f"GPIO_InitTypeDef {var};\n"
                        result += f"{var}.Pin = GPIO_PIN_{bit};\n"
                        result += f"{var}.Mode = GPIO_MODE_OUTPUT_PP;\n"
                        result += f"HAL_GPIO_Init(GPIO{port}, &{var});\n\n"
                elif "ESP32" in soc.upper():
                    call = f"gpio_set_direction(GPIO{config['pin_number']}, GPIO_MODE_OUTPUT);"
                    if call not in done:
                        done.add(call)
                        result += f"{call}\n\n"

            if len(configs) > 10:
                result += f"// ... and {len(configs) - 10} more GPIO pins\n"

            result += "```\n\n"

        if note := netlist_parser.pins_on_more_nets_note(
            dict.fromkeys(config["component"] for config in gpio_configs)
        ):
            result += f"{note}\n"
        return result

    except Exception as e:
        return error_answer(e, f"GPIO extraction of {schematic_path} failed")


@mcp.tool()
@with_schematic_versions("schematic_path")
@with_field_rules_note
async def extract_i2c_devices(
    schematic_path: str,
) -> str:
    """Give the I2C buses of a schematic and the devices on them (heuristic).

    An I2C net has "I2C" or "TWI" in its name. A device is a component with a pin on
    such a net whose value is in a small table of known devices (for example BMP280):
    the tool does not give the other components. An address in the net name (for
    example I2C_SDA_0x76) is the address of the device.

    Args:
        schematic_path: A .kicad_sch file of the project. The tool uses the
            components and the netlist of the full project.

    Returns:
        The I2C buses with their nets and devices.
    """
    try:
        sch_path = Path(schematic_path)
        if not sch_path.exists():
            return f"❌ **Schematic file not found:** {schematic_path}"

        # Obtain netlist
        from .netlist import current_netlist

        netlist_parser = await current_netlist(sch_path)

        # Extract I2C devices
        i2c_devices = []

        components = await components_of_project(sch_path, netlist_parser.kicad_version)

        for component in components:
            comp_ref = component.reference
            comp_value = component.value
            # Check if this is an I2C device
            for category, _bindings in DEVICE_TREE_BINDINGS.items():
                binding = _find_component_binding(comp_value, category)
                if binding:
                    nl_comp = netlist_parser.get_components().get(comp_ref)
                    for node in nl_comp.nodes if nl_comp else []:
                        if "I2C" in node.net.upper() or "TWI" in node.net.upper():
                            i2c_devices.append(
                                {
                                    "component": comp_ref,
                                    "value": comp_value,
                                    "compatible": binding["compatible"],
                                    "address": _extract_i2c_address_from_net(node.net),
                                    "net": node.net,
                                    "pin": node.number,
                                }
                            )
                            break

        if not i2c_devices:
            return f"""⚠️ **No I2C Devices Found**

**Schematic:** {schematic_path}

No I2C devices were found in the schematic.

**Next Steps:**
1. Ensure schematic has I2C peripherals
2. Check net names contain 'I2C' or 'TWI'
3. Include I2C addresses in net names (e.g., I2C_SDA_0x76)"""

        # Format results
        result = f"""# I2C Device Extraction

**Schematic:** {schematic_path}
**Total I2C Devices:** {len(i2c_devices)}

## I2C Device Details

| Component | Device | Compatible | Address | Net |
|-----------|--------|------------|---------|-----|
"""

        for device in i2c_devices:
            address_str = f"0x{device['address']:02X}" if device['address'] else "Unknown"
            result += f"| {device['component']} | {device['value']} | {device['compatible']} | {address_str} | {device['net']} |\n"

        # Add device tree generation
        result += "\n## Device Tree Configuration\n\n```dts\n"
        result += "&i2c1 {\n"
        result += "    status = \"okay\";\n\n"

        for device in i2c_devices:
            address_str = f"0x{device['address']:02X}" if device['address'] else "0x00"
            result += f"    {device['component'].lower()}: {device['compatible']}@{address_str} {{\n"
            result += f"        compatible = \"{device['compatible']}\";\n"
            result += f"        reg = <{address_str}>;\n"
            result += "    };\n\n"

        result += "};\n```"

        if note := netlist_parser.pins_on_more_nets_note(d["component"] for d in i2c_devices):
            result += f"\n\n{note}"
        return result

    except Exception as e:
        return error_answer(e, f"I2C extraction of {schematic_path} failed")


@mcp.tool()
@with_schematic_versions("schematic_path")
@with_field_rules_note
async def extract_spi_devices(
    schematic_path: str,
) -> str:
    """Give the SPI buses of a schematic and the devices on them (heuristic).

    An SPI net has "SPI" in its name. A device is a component with a pin on such a net
    whose value is in a small table of known devices: the tool does not give the
    other components.

    Args:
        schematic_path: A .kicad_sch file of the project. The tool uses the
            components and the netlist of the full project.

    Returns:
        The SPI buses with their nets and devices.
    """
    try:
        sch_path = Path(schematic_path)
        if not sch_path.exists():
            return f"❌ **Schematic file not found:** {schematic_path}"

        # Obtain netlist
        from .netlist import current_netlist

        netlist_parser = await current_netlist(sch_path)

        # Extract SPI devices
        spi_devices = []

        components = await components_of_project(sch_path, netlist_parser.kicad_version)

        for component in components:
            comp_ref = component.reference
            comp_value = component.value

            # Check if this is an SPI device
            for category, _bindings in DEVICE_TREE_BINDINGS.items():
                binding = _find_component_binding(comp_value, category)
                if binding:
                    nl_comp = netlist_parser.get_components().get(comp_ref)
                    for node in nl_comp.nodes if nl_comp else []:
                        if "SPI" in node.net.upper():
                            spi_devices.append(
                                {
                                    "component": comp_ref,
                                    "value": comp_value,
                                    "compatible": binding["compatible"],
                                    "net": node.net,
                                    "pin": node.number,
                                }
                            )
                            break

        if not spi_devices:
            return f"""⚠️ **No SPI Devices Found**

**Schematic:** {schematic_path}

No SPI devices were found in the schematic.

**Next Steps:**
1. Ensure schematic has SPI peripherals
2. Check net names contain 'SPI'
3. Include CS signals in net naming"""

        # Format results
        result = f"""# SPI Device Extraction

**Schematic:** {schematic_path}
**Total SPI Devices:** {len(spi_devices)}

## SPI Device Details

| Component | Device | Compatible | Net |
|-----------|--------|------------|-----|
"""

        for device in spi_devices:
            result += f"| {device['component']} | {device['value']} | {device['compatible']} | {device['net']} |\n"

        # Add device tree generation
        result += "\n## Device Tree Configuration\n\n```dts\n"
        result += "&spi1 {\n"
        result += "    status = \"okay\";\n\n"

        for i, device in enumerate(spi_devices):
            result += f"    {device['component'].lower()}: {device['compatible']}@{i} {{\n"
            result += f"        compatible = \"{device['compatible']}\";\n"
            result += f"        reg = <{i}>;\n"
            result += "        spi-max-frequency = <1000000>;\n"
            result += "    };\n\n"

        result += "};\n```"

        if note := netlist_parser.pins_on_more_nets_note(d["component"] for d in spi_devices):
            result += f"\n\n{note}"
        return result

    except Exception as e:
        return error_answer(e, f"SPI extraction of {schematic_path} failed")


@mcp.tool()
@with_schematic_versions("schematic_path")
@with_field_rules_note
async def extract_power_domains(
    schematic_path: str,
) -> str:
    """Give the regulators of a schematic, with a device tree part for each (heuristic).

    A regulator is a component whose value has a text of a list (for example REG,
    LDO, BUCK, BOOST, TPS, PMIC, also AP and LP: thus also, for example, CAP_10uF).
    The device tree part has a node for each of the first 5 regulators, with the
    voltage 3.3 V: correct it.

    Args:
        schematic_path: A .kicad_sch file of the project. The tool uses the
            components and the netlist of the full project.

    Returns:
        The regulators, and a device tree part with a node for each of the first
        5.
    """
    try:
        sch_path = Path(schematic_path)
        if not sch_path.exists():
            return f"❌ **Schematic file not found:** {schematic_path}"

        # Extract power components
        power_components = []

        components = await components_of_project(sch_path, None)

        for component in components:
            comp_ref = component.reference
            comp_value = component.value
            comp_library = component.library_id

            # Check if this is a power-related component
            power_keywords = ["REG", "LDO", "BUCK", "BOOST", "TPS", "AP", "LP", "AXP", "PMIC", "NPM"]
            if any(keyword in comp_value.upper() for keyword in power_keywords):
                power_components.append({
                    "reference": comp_ref,
                    "value": comp_value,
                    "library": comp_library,
                })

        if not power_components:
            return f"""⚠️ **No Power Components Found**

**Schematic:** {schematic_path}

No power management components were found in the schematic.

**Next Steps:**
1. Check for regulators, LDOs, power ICs
2. Verify component values contain power-related keywords"""

        # Format results
        result = f"""# Power Domain Extraction

**Schematic:** {schematic_path}
**Total Power Components:** {len(power_components)}

## Power Components

| Reference | Component | Type |
|-----------|-----------|------|
"""

        for comp in power_components:
            # Determine component type
            comp_type = "Regulator"
            if "LDO" in comp["value"].upper():
                comp_type = "LDO Regulator"
            elif "BUCK" in comp["value"].upper():
                comp_type = "Buck Converter"
            elif "BOOST" in comp["value"].upper():
                comp_type = "Boost Converter"
            elif "PMIC" in comp["value"].upper() or "NPM" in comp["value"].upper():
                comp_type = "PMIC"

            result += f"| {comp['reference']} | {comp['value']} | {comp_type} |\n"

        # Add device tree generation suggestions
        result += "\n## Device Tree Configuration\n\n```dts\n"
        result += "/ {\n"
        result += "    regulators {\n\n"

        for comp in power_components[:5]:  # Limit to first 5
            result += f"        {comp['reference'].lower()}: {comp['reference'].lower()} {{\n"
            result += "            compatible = \"regulator-fixed\";\n"
            result += "            regulator-name = \"" + comp['reference'].lower() + "\";\n"
            result += "            regulator-min-microvolt = <3300000>;\n"
            result += "            regulator-max-microvolt = <3300000>;\n"
            result += "        };\n\n"

        if len(power_components) > 5:
            result += "        // ... and " + str(len(power_components) - 5) + " more regulators\n"

        result += "    };\n"
        result += "};\n```"

        return result

    except Exception as e:
        return error_answer(e, f"Power domain extraction of {schematic_path} failed")


@mcp.tool()
@with_schematic_versions("schematic_path")
@with_field_rules_note
async def validate_pin_configuration(
    schematic_path: str,
) -> str:
    """Check a schematic before a device tree generation (heuristic).

    The tool gives the pin conflicts of detect_pin_conflicts: with an error (more than
    one driver pin on a net), it tells you not to make a device tree. It also looks
    for a microcontroller (STM32, ESP32, nRF or ATmega in the value). The tool only
    gives a report: generate_device_tree does not run this check. See
    docs/firmware.md of the repository for the limits.

    Args:
        schematic_path: A .kicad_sch file of the project. The tool uses the
            components and the netlist of the full project.

    Returns:
        The conflicts and the recommendations.
    """
    try:
        sch_path = Path(schematic_path)
        if not sch_path.exists():
            return f"❌ **Schematic file not found:** {schematic_path}"

        # First, find the pin conflicts. An error (more than one driver pin on a
        # net) stops the device tree. A warning (a net with one pin) does not.
        from .pin_analysis import _conflict_report, _pin_conflicts

        conflicts = await _pin_conflicts(sch_path)
        if any(c["severity"] == "error" for c in conflicts):
            return f"""❌ **Pin Configuration Validation Failed**

The schematic has pin conflicts that must be resolved before
device tree generation.

{_conflict_report(schematic_path, conflicts)}

## Device Tree Generation Blocked

Pin conflicts prevent reliable device tree generation. Please
resolve all conflicts before attempting device tree generation."""

        # Check for MCU components
        components = await components_of_project(sch_path, None)

        mcu_found = False
        for component in components:
            comp_value = component.value
            if any(soc in comp_value.upper() for soc in ["STM32", "ESP32", "NRF", "ATMEGA"]):
                mcu_found = True
                break

        if not mcu_found:
            return f"""⚠️ **No MCU Component Found**

**Schematic:** {schematic_path}

Device tree generation requires an MCU component in the schematic.

**Supported MCU families:**
- STM32 (STM32F, STM32H, STM32L series)
- ESP32 (ESP32, ESP32-S2, ESP32-S3, etc.)
- nRF52 (nRF52832, nRF52840, etc.)
- ATmega (ATmega328P, ATmega2560, etc.)

**Next Steps:**
1. Add an MCU component to the schematic
2. Verify component value matches supported patterns
3. Re-run validation"""

        warnings = sum(1 for c in conflicts if c["severity"] == "warning")
        single_pin_note = (
            f"\n- ⚠️ {warnings} net(s) with one pin: see detect_pin_conflicts" if warnings else ""
        )
        return f"""✅ **Pin Configuration Validation Passed**

**Schematic:** {schematic_path}

The schematic is ready for device tree generation.

## Validation Results

- ✅ No net with more than one driver pin
- ✅ MCU component found{single_pin_note}

## Ready for Device Tree Generation

You can now proceed with device tree generation:

```bash
# Generate device tree
generate_device_tree(
    schematic_path="{schematic_path}",
    target_soc="stm32f4",
    output_path="{sch_path.stem}.dts"
)
```

**Supported SOC families:** {', '.join(DEVICE_TREE_TEMPLATES.keys())}"""

    except Exception as e:
        return error_answer(e, f"Validation of {schematic_path} failed")
