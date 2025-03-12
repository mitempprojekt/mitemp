#!/usr/bin/env python3
import asyncio
import configparser
import logging
import subprocess

from bleak import BleakClient
from prometheus_client import CollectorRegistry, Gauge, push_to_gateway

# UUID für die Xiaomi Mi Temperature 2 Sensordateneigenschaft.
CHAR_UUID = "ebe0ccc1-7a0a-4b0c-8a1a-6ff2997da3a6"

def force_disconnect(mac_address: str):
    #Trennt Verbindung falls Skript vorher unsauber beendet wurde
    logging.info(f"Trenne Verbindung für Gerät {mac_address}")
    try:
        result = subprocess.run(
            ["bluetoothctl", "disconnect", mac_address],
            capture_output=True,
            text=True,
            check=True
        )
        logging.info(f"bluetoothctl Ausgabe: {result.stdout.strip()}")
    except subprocess.CalledProcessError as e:
        # Loggen wenn keine Verbindung besteht
        logging.info(f"Sensor {mac_address} hat keine offene Verbindung. ({e.stderr.strip()})")
    except Exception as e:
        logging.error(f"Unerwarteter Fehler beim Trennen der Verbindung für {mac_address}: {e}")

async def read_sensor(address: str, retries: int = 3, delay: float = 5.0):
    
    #Versucht, Sensordaten über Benachrichtigungen auszulesen
    #Bei Bedarf erneuter Verbindunsaufbau
    
    for attempt in range(retries):
        try:
            # Erstelle ein Future, um auf Benachrichtigungsdaten zu warten.
            loop = asyncio.get_running_loop()
            data_future = loop.create_future()

            def notification_handler(sender, data):
                if not data_future.done():
                    data_future.set_result(data)

            async with BleakClient(address) as client:
                await client.start_notify(CHAR_UUID, notification_handler)
                try:
                    # Warte auf Benachrichtigung (Timeout nach 20 Sekunden)
                    data = await asyncio.wait_for(data_future, timeout=20.0)
                except asyncio.TimeoutError:
                    await client.stop_notify(CHAR_UUID)
                    raise TimeoutError(f"Zeitüberschreitung für Sensor {address}")
                await client.stop_notify(CHAR_UUID)

                if len(data) != 5:
                    raise ValueError(f"Erwartet werden 5 Bytes von Sensor {address}, aber {len(data)} erhalten")

                # Sensordaten einlesen:
                # - Bytes 1 und 2: Temperatur (in Celsius, little-endian, signed, geteilt durch 100)
                # - Byte 3: Feuchtigkeit (in Prozent)
                # - Byte 4 und 5: Batteriespannung (in Millivolt, little-endian)
                temp_raw = int.from_bytes(data[0:2], byteorder="little", signed=True)
                temperature = temp_raw / 100.0
                humidity = data[2]
                battery = int.from_bytes(data[3:5], byteorder="little")
                return temperature, humidity, battery
        except Exception as e:
            logging.error(f"Versuch {attempt + 1} für Sensor {address} fehlgeschlagen: {e}")
            if attempt < retries - 1:
                await asyncio.sleep(delay)
            else:
                raise

async def process_sensor(sensor_name: str, address: str, pushgateway_url: str):
    
    #Liest Sensordaten über Benachrichtigungen aus, protokolliert sie und sendet
    #die Metriken an das Prometheus Pushgateway.
    
    temperature, humidity, battery = await read_sensor(address)
    logging.info(f"{sensor_name}: Temp={temperature} °C, Feuchtigkeit={humidity} %, Batterie={battery}")

    # Erstellt eine neue Prometheus-Registry für den Sensor.
    registry = CollectorRegistry()
    gauge_temp = Gauge('sensor_temperature_celsius', 'Temperatur in Celsius', ['sensor'], registry=registry)
    gauge_hum = Gauge('sensor_humidity_percent', 'Relative Luftfeuchtigkeit in Prozent', ['sensor'], registry=registry)
    gauge_battery = Gauge('sensor_battery_raw', 'Batteriespannung in mV', ['sensor'], registry=registry)

    # Setzt die Metriken.
    gauge_temp.labels(sensor=sensor_name).set(temperature)
    gauge_hum.labels(sensor=sensor_name).set(humidity)
    gauge_battery.labels(sensor=sensor_name).set(battery)

    # Sendet die Metriken an das Pushgateway
    push_to_gateway(pushgateway_url, job='xiaomi_sensor', grouping_key={'sensor': sensor_name}, registry=registry)
    logging.info(f"Metriken von {sensor_name} an {pushgateway_url} gesendet")

async def sensor_loop(sensor_name: str, address: str, pushgateway_url: str):
    
    # Liest kontinuierlich Sensordaten aus und sendet sie
    
    while True:
        try:
            await process_sensor(sensor_name, address, pushgateway_url)
        except Exception as e:
            logging.error(f"Fehler in sensor_loop für {sensor_name} bei {address}: {e}")
        # Warte 15 Sekunden, bevor erneut versucht wird (anpassbar)
        await asyncio.sleep(15)

async def main():
    # Liest die Sensor-Konfiguration aus der .ini-Datei
    config = configparser.ConfigParser()
    config.read("sensors.ini")

    #  URL des Pushgateway setzen
    if 'pushgateway' in config:
        pushgateway_url = config['pushgateway'].get('url', 'http://localhost:9091')
    else:
        pushgateway_url = 'http://localhost:9091'

    sensor_addresses = []
    # Alle Sektionen außer 'pushgateway' werden als Sensoren interpretiert
    for section in config.sections():
        if section.lower() == 'pushgateway':
            continue
        sensor_name = config[section].get('name', section)
        address = config[section].get('address')
        if not address:
            logging.warning(f"Sektion {section} enthält keine Adresse.")
            continue
        sensor_addresses.append((sensor_name, address))
    
    # Trennt alle Sensor-Verbindungen einmalig beim Start des Containers
    for sensor_name, address in sensor_addresses:
        force_disconnect(address)
    
    # Startet für jeden Sensor eine Endlosschleife
    tasks = []
    for sensor_name, address in sensor_addresses:
        tasks.append(sensor_loop(sensor_name, address, pushgateway_url))
    
    # Führt alle Sensor-Schleifen gleichzeitig aus
    await asyncio.gather(*tasks, return_exceptions=True)

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
