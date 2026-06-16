#!/usr/bin/env python3
import argparse
import asyncio
import os
import sys
from io import BytesIO
from bleak import BleakScanner, BleakClient

# Try to import Pillow for format conversions (e.g., PNG to JPEG) and scaling
try:
    from PIL import Image
except ImportError:
    Image = None

# Custom UUIDs for HP LPP Service & Characteristics
SERVICE_UUID = "6822d239-7b61-4718-bdc1-189221946209"
TX_CHAR_UUID = "6822d239-7b61-4718-bdc1-de55b3f9051e"
RX_CHAR_UUID = "6822d239-7b61-4718-bdc1-772fa9983658"
PAIRING_CHAR_UUID = "6822d239-7b61-4718-bdc1-3dd5acdd2eee"

class Colors:
    HEADER = '\033[95m'
    OKBLUE = '\033[94m'
    OKCYAN = '\033[96m'
    OKGREEN = '\033[92m'
    WARNING = '\033[93m'
    FAIL = '\033[91m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'

def print_success(msg):
    print(f"✨ {Colors.OKGREEN}{msg}{Colors.ENDC}")

def print_info(msg):
    print(f"ℹ️  {Colors.OKCYAN}{msg}{Colors.ENDC}")

def print_warning(msg):
    print(f"⚠️  {Colors.WARNING}{msg}{Colors.ENDC}")

def print_error(msg):
    print(f"❌ {Colors.FAIL}{Colors.BOLD}Error:{Colors.ENDC} {Colors.FAIL}{msg}{Colors.ENDC}", file=sys.stderr)


ERROR_CODES = {
    1: "UNSPECIFIED_ERROR",
    2: "TRANSPORT_ERROR",
    3: "UNAUTHORIZED_ERROR",
    4: "COMMAND_NOT_SUPPORTED_ERROR",
    5: "NOT_FOUND_ERROR",
    6: "INVALID_ARGUMENTS_ERROR",
    7: "INVALID_LENGTH_ERROR",
    8: "ILLEGAL_STATE_ERROR",
    9: "INSUFFICIENT_RESOURCES_ERROR",
    10: "IF_MISMATCH_ERROR",
    11: "CHECK_FAILED_ERROR",
    12: "NOT_ALLOWED_ERROR",
    13: "TIMEOUT_ERROR",
    14: "MALFORMED_MESSAGE_ERROR",
    15: "OPERATION_NOT_SUPPORTED_ERROR",
    16: "OPERATION_FAILED_ERROR",
    17: "BATTERY_LOW_ERROR",
    18: "RETRY_ERROR"
}

PRINT_STATUS_LABELS = {
    1: "Idle",
    2: "Preparing",
    3: "Out of Paper",
    4: "Paper Jam",
    5: "Calibrating",
    6: "Tray Open",
    7: "Printing",
    8: "Overheating",
    9: "Feed Path Obstructed",
    10: "Out of Supplies",
    11: "No Supplies Detected",
    12: "No Tray",
    13: "Tray Misaligned",
    14: "Unrecoverable Error",
    15: "Battery Critical",
    16: "Paper Pick Failed",
    17: "Multiple Pages Picked"
}

BATTERY_STATUS_LABELS = {
    1: "In Use (Discharging)",
    2: "Charging",
    3: "Heat Protection",
    4: "Battery Error"
}

SUPPLY_TYPE_LABELS = {
    1: "Toppan Ink Cartridge",
    255: "Unrecognized / None"
}

CAP_STATUS_LABELS = {
    0: "Capped (Cover Closed)",
    1: "Uncapped (Cover Open)"
}

QUEUE_STATUS_LABELS = {
    1: "Empty",
    2: "Full",
    3: "Paused",
    4: "Populated",
    5: "Error"
}


class HPLPPClient:
    """Implements HP LPP (Light Weight Print Protocol) on top of BleakClient."""
    def __init__(self, client: BleakClient):
        self.client = client
        self.mtu = 20  # Default initial MTU
        self.upstream_ack_period = 0
        self.receive_buffer = bytearray()
        self.expected_seq = 1
        self.response_events = {}

    def rx_notification_handler(self, sender, data):
        """Processes incoming data packages and performs HPLPP reassembly."""
        if not data:
            return
        
        header = data[0]
        if header == 0:
            # Downstream ACK
            return

        seq = header & 0x7F
        is_last = bool(header & 0x80)
        payload = data[1:]

        if seq == self.expected_seq:
            self.receive_buffer.extend(payload)
            self.expected_seq += 1

            if is_last:
                # HPLPP Message fully reassembled
                assembled = bytes(self.receive_buffer)
                self.process_received_message(assembled)
                self.receive_buffer.clear()
                self.expected_seq = 1
        else:
            print_warning(f"Sequence mismatch: Expected {self.expected_seq}, got {seq}. Resetting buffer.")
            self.receive_buffer.clear()
            self.expected_seq = 1

    def process_received_message(self, message):
        """Parses fully reassembled HPLPP messages and triggers awaiting events."""
        if not message:
            return
        cmd_code = message[0]
        payload = message[1:]

        if cmd_code == 1:  # ERROR command from printer
            failed_cmd = payload[0] if len(payload) > 0 else 0
            err_code = payload[1] if len(payload) > 1 else 0
            err_str = ERROR_CODES.get(err_code, f"UNKNOWN ({err_code})")
            print_error(f"Printer returned ERROR response for command {failed_cmd}: {err_str}")
            
            # Wake up the waiting command with an error payload
            expected_rsp = failed_cmd + 1
            # Special case mapping for WR_JOB_PROP_REQ (48 -> 49)
            if failed_cmd == 48:
                expected_rsp = 49
                
            if expected_rsp in self.response_events:
                event, _ = self.response_events[expected_rsp]
                self.response_events[expected_rsp] = (event, b"ERROR:" + bytes([err_code]))
                event.set()
            return

        if cmd_code in self.response_events:
            event, _ = self.response_events[cmd_code]
            self.response_events[cmd_code] = (event, payload)
            event.set()

    async def send_and_wait(self, cmd_code, wait_cmd_code, payload=b"", timeout=10.0):
        """Registers a response event, sends an HPLPP message, and awaits the response to prevent races."""
        event = asyncio.Event()
        self.response_events[wait_cmd_code] = (event, None)
        try:
            await self.send_hplpp_message(cmd_code, payload)
            await asyncio.wait_for(event.wait(), timeout=timeout)
            _, response_payload = self.response_events[wait_cmd_code]
            
            if response_payload and response_payload.startswith(b"ERROR:"):
                err_code = response_payload[6]
                err_str = ERROR_CODES.get(err_code, f"UNKNOWN ({err_code})")
                raise Exception(f"Printer returned error status: {err_str}")
                
            return response_payload
        except asyncio.TimeoutError:
            return None
        finally:
            self.response_events.pop(wait_cmd_code, None)

    async def send_hplpp_message(self, cmd_code, payload=b""):
        """Segments and transmits an HPLPP message over BLE using write-with-response."""
        msg = bytes([cmd_code]) + payload
        seq = 1
        i = 0
        while i < len(msg):
            rem = len(msg) - i
            chunk_size = self.mtu - 1
            if rem <= chunk_size:
                # Last packet of the message
                header = seq | 0x80
                packet = bytes([header]) + msg[i:]
                i = len(msg)
            else:
                # Middle packet
                header = seq
                packet = bytes([header]) + msg[i : i + chunk_size]
                i += chunk_size
            seq += 1
            
            await self.client.write_gatt_char(TX_CHAR_UUID, packet, response=True)
            # Add a small delay between writes to allow the printer's BLE chip to process the packets
            await asyncio.sleep(0.005)

    async def perform_handshake(self):
        """Subscribes to notifications and performs the interface configuration handshake."""
        # 1. Subscribe to RX Characteristic notifications
        print_info("Subscribing to printer notifications...")
        await self.client.start_notify(RX_CHAR_UUID, self.rx_notification_handler)
        
        # Give macOS BLE stack a brief moment to settle the notification setup
        await asyncio.sleep(0.5)

        # 2. Send BLE Interface Config Request and wait for BLEIFConfigResponseMessage (0x0B)
        print_info("Negotiating protocol handshake...")
        response = await self.send_and_wait(0x0A, 0x0B, b"\x01\x00", timeout=10.0)
        if not response:
            raise Exception("No response to interface configuration request.")

        # Parse BLEIFConfigResponseMessage
        mtu = int.from_bytes(response[1:3], byteorder="little")
        upstream_ack = response[3]
        
        # Clamp MTU to the maximum supported by Bleak/macOS connection to prevent fragmentation overruns
        try:
            bleak_max_payload = self.client.mtu_size - 3
            self.mtu = min(mtu, bleak_max_payload)
            print_info(f"Handshake parsed printer MTU: {mtu}. Bleak connection MTU: {self.client.mtu_size}. Operating MTU: {self.mtu}")
        except Exception:
            self.mtu = mtu
            print_info(f"Handshake parsed printer MTU: {mtu}. Operating MTU: {self.mtu}")
            
        self.upstream_ack_period = upstream_ack

        # 3. Perform connection setup to negotiate maximum target message size (Command Code: 0x24 -> 0x25)
        print_info("Performing connection setup...")
        conn_setup_payload = b"\x00\x10"  # maxHostMessageSize = 4096 (little-endian short: 0x00 0x10)
        conn_rsp = await self.send_and_wait(0x24, 0x25, conn_setup_payload, timeout=10.0)
        if not conn_rsp:
            raise Exception("No response to connection setup request.")
        
        # Parse ConnSetupResponseMessage
        self.max_target_msg_size = int.from_bytes(conn_rsp[0:2], byteorder="little")
        security_level = conn_rsp[2]
        print_success(f"Connection setup complete. Max Target Message Size: {self.max_target_msg_size}, Security Level: {security_level}")

    def read_variable_length(self, data, offset):
        val = 0
        shift = 0
        while True:
            b = data[offset]
            offset += 1
            val |= (b & 0x7F) << shift
            if not (b & 0x80):
                break
            shift += 7
        return val, offset

    def read_string(self, data, offset):
        length, offset = self.read_variable_length(data, offset)
        string_bytes = data[offset:offset+length]
        offset += length
        return string_bytes.decode('utf-8', errors='ignore'), offset

    async def read_status(self):
        """Requests and parses the printer status fields supported by the firmware (Command Code: 0x08 -> 0x09)."""
        # Request all status fields supported by the PrintMaker firmware. Requesting unsupported fields causes an INVALID_ARGUMENTS_ERROR.
        fields = [1, 2, 3, 4, 5, 6, 10, 11, 14]
        payload = bytes(fields)
        print_info("Reading printer status fields...")
        response = await self.send_and_wait(0x08, 0x09, payload, timeout=5.0)
        if not response:
            raise Exception("No response to status request.")
        
        status = {}
        offset = 0
        while offset < len(response):
            field_id = response[offset]
            offset += 1
            
            if field_id == 1:    # SYSTEM_FLAGS
                val = int.from_bytes(response[offset:offset+4], byteorder="little")
                offset += 4
                status["system_flags"] = {
                    "value": val,
                    "time_invalid": bool(val & 1),
                    "battery_critical": bool(val & 2),
                    "print_busy": bool(val & 4),
                    "out_of_paper": bool(val & 8),
                    "low_on_supplies": bool(val & 16),
                    "low_battery": bool(val & 32)
                }
            elif field_id == 2:  # PRINT_STATUS
                status["print_status"] = response[offset]
                offset += 1
            elif field_id == 3:  # BATTERY_LEVEL
                status["battery_level"] = response[offset]
                offset += 1
            elif field_id == 4:  # PRINT_PROGRESS
                status["print_progress"] = response[offset]
                offset += 1
            elif field_id == 5:  # CURRENT_JOB
                status["current_job"] = int.from_bytes(response[offset:offset+2], byteorder="little")
                offset += 2
            elif field_id == 6:  # BATTERY_STATUS
                status["battery_status"] = response[offset]
                offset += 1
            elif field_id == 7:  # QUEUE_STATUS
                status["queue_status"] = response[offset]
                offset += 1
            elif field_id == 8:  # CURRENT_JOB_COPY_PROGRESS
                status["current_job_copy_progress"] = response[offset]
                offset += 1
            elif field_id == 9:  # NUMBER_OF_HOSTS
                status["number_of_hosts"] = response[offset]
                offset += 1
            elif field_id == 10: # SUPPLY_TYPE
                status["supply_type"] = response[offset]
                offset += 1
            elif field_id == 11: # SUPPLY_LEVEL
                status["supply_level"] = response[offset]
                offset += 1
            elif field_id == 12: # SUPPLY_VERSION
                status["supply_version"] = int.from_bytes(response[offset:offset+2], byteorder="little")
                offset += 2
            elif field_id == 13: # SUPPLY_SELECTABILITY
                val, offset = self.read_string(response, offset)
                status["supply_selectability"] = val
            elif field_id == 14: # CAP_STATUS
                status["cap_status"] = response[offset]
                offset += 1
            else:
                print_warning(f"Unknown status field code received: {field_id}")
                break
        return status

    async def print_image(self, jpeg_data, copies=1):
        """Orchestrates the HPLPP print lifecycle to transmit the JPEG payload."""
        file_len = len(jpeg_data)
        
        # 1. Send PRINT_START_REQ (0x0C) and wait for PRINT_START_RSP (0x0D)
        print_info(f"Initializing print job. Sending JPEG size: {file_len} bytes...")
        start_payload = b"\x01" + file_len.to_bytes(4, byteorder="little")
        start_rsp = await self.send_and_wait(0x0C, 0x0D, start_payload, timeout=10.0)
        if not start_rsp:
            raise Exception("Failed to start print: No response received.")
        
        file_handle = start_rsp[0]
        job_id = int.from_bytes(start_rsp[1:3], byteorder="little")
        print_info(f"Job created. ID: {job_id}, Handle: {file_handle}")

        # 3. Send WR_JOB_PROP_REQ (0x30) and wait for WR_JOB_PROP_RSP (0x31)
        prop_payload = job_id.to_bytes(2, byteorder="little") + b"\x03" + bytes([copies])
        prop_rsp = await self.send_and_wait(0x30, 0x31, prop_payload, timeout=5.0)
        if prop_rsp is None:
            raise Exception("Failed to set print job properties.")

        # 5. Write file data chunks using FILE_WRITE_REQ (0x0E)
        chunk_size = self.max_target_msg_size - 2
        sent_bytes = 0

        print_info("Streaming print data to the printer...")
        while sent_bytes < file_len:
            chunk = jpeg_data[sent_bytes : sent_bytes + chunk_size]
            write_payload = bytes([file_handle]) + chunk
            write_rsp = await self.send_and_wait(0x0E, 0x0F, write_payload, timeout=10.0)
            if not write_rsp:
                raise Exception(f"File write timed out at byte {sent_bytes}/{file_len}")

            status = write_rsp[1]
            rec_len = int.from_bytes(write_rsp[2:6], byteorder="little")

            if status == 1:
                # OK / Printing
                sent_bytes = rec_len
                progress = (sent_bytes / file_len) * 100
                print(f"  └─ Progress: {progress:.1f}% ({sent_bytes}/{file_len} bytes)", end="\r")
            elif status == 2:
                # Complete
                print()
                print_success("Print job fully completed!")
                break
            elif status == 3:
                print()
                raise Exception("Print job cancelled by printer.")
            elif status == 4:
                print()
                raise Exception("Print job failed on printer.")
            else:
                print()
                raise Exception(f"Unknown printer status code: {status}")


async def scan_for_printer():
    """Scans for BLE devices advertising HPLPP service."""
    print_info("Scanning for PrintMaker printers...")
    try:
        devices_and_adv = await BleakScanner.discover(timeout=5.0, return_adv=True)
        matches = []
        for address, (device, adv) in devices_and_adv.items():
            uuids = [u.lower() for u in adv.service_uuids]
            name = adv.local_name or device.name or ""
            if SERVICE_UUID.lower() in uuids or "printmaker" in name.lower() or "hp" in name.lower():
                matches.append(device)
        return matches
    except Exception as e:
        print_warning(f"Advanced scan failed: {e}. Trying fallback scan...")
        devices = await BleakScanner.discover(timeout=5.0)
        matches = []
        for d in devices:
            uuids = []
            if hasattr(d, "metadata") and isinstance(d.metadata, dict):
                uuids = d.metadata.get("uuids", [])
            uuids = [str(u).lower() for u in uuids]
            name = d.name or ""
            if SERVICE_UUID.lower() in uuids or "printmaker" in name.lower() or "hp" in name.lower():
                matches.append(d)
        return matches

def display_printer_info(status):
    """Formats and prints the printer status dictionary in a clean, visual layout."""
    print("\n" + "="*50)
    print(f"{Colors.BOLD}🖨️  PRINTMAKER PRINTER STATUS{Colors.ENDC}")
    print("="*50)
    
    # 1. System Flags (handled under Alerts section at the bottom)
    
    # 2. Print Status
    if "print_status" in status:
        print_stat = status["print_status"]
        print_stat_str = PRINT_STATUS_LABELS.get(print_stat, f"Unknown ({print_stat})")
        print(f"📈 {Colors.BOLD}Printer State:{Colors.ENDC} {print_stat_str}")
    else:
        print(f"📈 {Colors.BOLD}Printer State:{Colors.ENDC} Not reported by printer")
        
    # 3. Battery Level & 6. Battery Status
    if "battery_level" in status or "battery_status" in status:
        bat_level = status.get("battery_level", "Unknown")
        bat_stat = status.get("battery_status", 0)
        bat_stat_str = BATTERY_STATUS_LABELS.get(bat_stat, f"Unknown ({bat_stat})")
        print(f"🔋 {Colors.BOLD}Battery Level:{Colors.ENDC} {bat_level}% ({bat_stat_str})")
    else:
        print(f"🔋 {Colors.BOLD}Battery Status:{Colors.ENDC} Not reported by printer")

    # 4. Print Progress
    if "print_progress" in status:
        print(f"📊 {Colors.BOLD}Print Progress:{Colors.ENDC} {status['print_progress']}%")
    else:
        print(f"📊 {Colors.BOLD}Print Progress:{Colors.ENDC} Not reported by printer")

    # 5. Current Job ID
    if "current_job" in status:
        print(f"🆔 {Colors.BOLD}Current Job ID:{Colors.ENDC} {status['current_job']}")
    else:
        print(f"🆔 {Colors.BOLD}Current Job ID:{Colors.ENDC} Not reported by printer")
        
    # 7. Queue Status
    if "queue_status" in status:
        q_status = status["queue_status"]
        q_status_str = QUEUE_STATUS_LABELS.get(q_status, f"Unknown ({q_status})")
        print(f"📋 {Colors.BOLD}Queue State:{Colors.ENDC} {q_status_str}")
    else:
        print(f"📋 {Colors.BOLD}Queue State:{Colors.ENDC} Not reported by printer")

    # 8. Current Job Copy Progress
    if "current_job_copy_progress" in status:
        print(f"📄 {Colors.BOLD}Job Copy Progress:{Colors.ENDC} {status['current_job_copy_progress']}%")
    else:
        print(f"📄 {Colors.BOLD}Job Copy Progress:{Colors.ENDC} Not reported by printer")

    # 9. Number of Hosts
    if "number_of_hosts" in status:
        print(f"👥 {Colors.BOLD}Connected Hosts:{Colors.ENDC} {status['number_of_hosts']}")
    else:
        print(f"👥 {Colors.BOLD}Connected Hosts:{Colors.ENDC} Not reported by printer")

    # 10. Supply Type & 11. Supply Level
    if "supply_level" in status or "supply_type" in status:
        ink_level = status.get("supply_level", "Unknown")
        supply_type = status.get("supply_type", 0)
        supply_type_str = SUPPLY_TYPE_LABELS.get(supply_type, f"Unknown ({supply_type})")
        print(f"💧 {Colors.BOLD}Ink Level:{Colors.ENDC} {ink_level}% ({supply_type_str})")
    else:
        print(f"💧 {Colors.BOLD}Ink/Supply Status:{Colors.ENDC} Not reported by printer")

    # 12. Supply Version
    if "supply_version" in status:
        print(f"🏷️  {Colors.BOLD}Supply Version:{Colors.ENDC} {status['supply_version']}")
    else:
        print(f"🏷️  {Colors.BOLD}Supply Version:{Colors.ENDC} Not reported by printer")

    # 13. Supply Selectability
    if "supply_selectability" in status:
        print(f"📝 {Colors.BOLD}Supply Selectability:{Colors.ENDC} {status['supply_selectability']}")
    else:
        print(f"📝 {Colors.BOLD}Supply Selectability:{Colors.ENDC} Not reported by printer")

    # 14. Cap Status (Printhead Cover)
    if "cap_status" in status:
        cap_status = status["cap_status"]
        cap_status_str = CAP_STATUS_LABELS.get(cap_status, f"Unknown ({cap_status})")
        print(f"🔒 {Colors.BOLD}Printhead Cover:{Colors.ENDC} {cap_status_str}")
    else:
        print(f"🔒 {Colors.BOLD}Printhead Cover:{Colors.ENDC} Not reported by printer")
        
    # System Flags / Alerts (1. SYSTEM_FLAGS)
    if "system_flags" in status:
        flags = status["system_flags"]
        print("\n⚠️  " + Colors.BOLD + "Active Alerts / System Flags:" + Colors.ENDC)
        alerts = []
        if flags.get("time_invalid"):
            alerts.append("Device system time is invalid/not set.")
        if flags.get("battery_critical"):
            alerts.append("Battery is critically low!")
        if flags.get("low_battery"):
            alerts.append("Battery is low.")
        if flags.get("low_on_supplies"):
            alerts.append("Ink supply is low.")
        if flags.get("out_of_paper"):
            alerts.append("Out of paper.")
        if flags.get("print_busy"):
            alerts.append("Printer is busy.")
        
        if alerts:
            for alert in alerts:
                print(f"  ├─ {Colors.WARNING}{alert}{Colors.ENDC}")
        else:
            print(f"  └─ {Colors.OKGREEN}All systems clear. No warnings.{Colors.ENDC}")
    else:
        print("\n⚠️  " + Colors.BOLD + "Active Alerts / System Flags:" + Colors.ENDC + " Not reported by printer")
            
    print("="*50 + "\n")


async def run_print_flow(args):
    # Target Selection
    target_address = args.device
    if not target_address:
        printers = await scan_for_printer()
        if not printers:
            print_error("No PrintMaker printers found. Ensure the printer is turned on and in pairing mode.")
            return
        elif len(printers) == 1:
            target_address = printers[0].address
            print_info(f"Found printer: {printers[0].name} ({target_address})")
        else:
            print("\nMultiple PrintMaker printers found:")
            for idx, p in enumerate(printers):
                print(f"  [{idx}] {p.name} ({p.address})")
            try:
                choice = int(input("\nSelect a printer index: "))
                target_address = printers[choice].address
            except Exception:
                print_error("Invalid selection.")
                return

    # Connect to BLE client
    print_info(f"Connecting to device: {target_address}...")
    async with BleakClient(target_address) as client:
        if not client.is_connected:
            print_error("Connection failed.")
            return
        
        print_success("Connected!")
        hplpp = HPLPPClient(client)
        await hplpp.perform_handshake()
        
        # 1. Retrieve and Display Printer Status if requested
        if args.info:
            try:
                status = await hplpp.read_status()
                display_printer_info(status)
            except Exception as e:
                print_error(f"Failed to retrieve status info: {e}")
                
        # 2. Print Image if provided
        if args.image:
            # Validate and process image
            if not os.path.isfile(args.image):
                print_error(f"File not found: {args.image}")
                return

            # Check extension and convert to JPEG if necessary using Pillow
            _, ext = os.path.splitext(args.image.lower())
            jpeg_data = None

            if ext in [".jpg", ".jpeg"] and not args.force_scale:
                # If Pillow is loaded and force_scale is not requested, we check dimensions or read raw bytes
                # But to guarantee print compatibility, it's safer to always scale to height=150px if Pillow is available.
                if Image:
                    try:
                        img = Image.open(args.image)
                        if img.height != 150:
                            print_info(f"Scaling image height from {img.height}px to 150px (maintaining aspect ratio)...")
                            aspect = img.width / img.height
                            new_width = int(150 * aspect)
                            img = img.resize((new_width, 150), Image.Resampling.LANCZOS)
                            buf = BytesIO()
                            img.save(buf, format="JPEG", quality=100)
                            jpeg_data = buf.getvalue()
                        else:
                            with open(args.image, "rb") as f:
                                jpeg_data = f.read()
                    except Exception as e:
                        print_warning(f"Failed to check/resize image with Pillow: {e}. Fallback to sending raw bytes.")
                        with open(args.image, "rb") as f:
                            jpeg_data = f.read()
                else:
                    with open(args.image, "rb") as f:
                        jpeg_data = f.read()
            else:
                # Needs conversion or resizing
                if not Image:
                    print_error(f"Pillow library is required to process '{ext}' images. Please run: pip install Pillow")
                    return
                try:
                    img = Image.open(args.image)
                    img = img.convert("RGB")  # Convert transparent PNG etc. to RGB
                    
                    # Scale to 150px height matching the physical printhead resolution
                    if img.height != 150 or args.force_scale:
                        print_info(f"Scaling image height from {img.height}px to 150px (maintaining aspect ratio)...")
                        aspect = img.width / img.height
                        new_width = int(150 * aspect)
                        img = img.resize((new_width, 150), Image.Resampling.LANCZOS)
                        
                    buf = BytesIO()
                    img.save(buf, format="JPEG", quality=100)
                    jpeg_data = buf.getvalue()
                except Exception as e:
                    print_error(f"Failed to convert/resize image: {e}")
                    return

            # Print image
            await hplpp.print_image(jpeg_data, copies=args.copies)


def main():
    parser = argparse.ArgumentParser(
        description="Print image files directly to the PrintMaker Bluetooth handheld printer.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  python3 print_image.py --info
  python3 print_image.py photo.jpg
  python3 print_image.py sticker.png --copies 2
  python3 print_image.py photo.jpg -d 00:11:22:33:44:55"""
    )
    
    parser.add_argument("image", nargs="?", help="Path to the image file (JPEG or PNG). Optional if --info is specified.")
    parser.add_argument("-d", "--device", help="Bluetooth MAC Address/UUID of the printer. If omitted, performs a scan.")
    parser.add_argument("-c", "--copies", type=int, default=1, help="Number of copies to print (default: 1).")
    parser.add_argument("--info", action="store_true", help="Retrieve and print printer status information (ink level, battery, etc.) and exit.")
    parser.add_argument("--force-scale", action="store_true", help="Force scaling of the image height to 150px even if it is already JPEG.")

    args = parser.parse_args()
    
    if not args.info and not args.image:
        parser.print_help()
        return
    
    try:
        asyncio.run(run_print_flow(args))
    except KeyboardInterrupt:
        print_warning("\nPrint job cancelled by user.")
    except Exception as e:
        print_error(f"Error during print operation: {e}")

if __name__ == "__main__":
    main()

