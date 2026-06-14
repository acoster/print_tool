# PrintMaker BLE CLI Print Tool

A custom Python CLI tool for macOS to scan, connect, handshake, and print images directly to the **PrintMaker** handheld Bluetooth printer.

This tool implements the proprietary **HP LPP (Light Weight Print Protocol)** over BLE (Bluetooth Low Energy) framing, segmentation, and commands extracted from the decompiled printer application.

## Requirements

1. **Python 3.8+**
2. **macOS Bluetooth Permissions**: The first time you run a script that uses Bluetooth on macOS, the operating system will show a system permission dialog. Ensure Bluetooth is enabled on your Mac and in your terminal emulator.
3. **Printer Status**: Turn on your PrintMaker printer and ensure it is not connected to any other device (active pairing mode).

## Setup Instructions

We recommend running the tool in a Python virtual environment:

```bash
# 1. Navigate to the tool directory
cd /Users/alex/.gemini/antigravity-ide/scratch/print_tool

# 2. Create a virtual environment
python3 -m venv venv

# 3. Activate the virtual environment
source venv/bin/activate

# 4. Install dependencies
pip install -r requirements.txt
```

## How to Use

### 1. Simple Print (Auto-scan)
Pass the image file directly. The script will automatically scan for PrintMaker BLE printers and start printing if one is found:
```bash
./print_image.py photo.jpg
```
If multiple printers are found, it will print a list and prompt you to select one.

### 2. Print with Format Conversion
The printer natively expects JPEG image data. If you pass a PNG, BMP, or other format, the script automatically uses the `Pillow` library to convert it into a compliant RGB JPEG byte stream before sending:
```bash
./print_image.py label.png
```

### 3. Print to a Specific Printer Address
To skip scanning and connect immediately, pass the BLE MAC Address (or macOS UUID):
```bash
./print_image.py photo.jpg -d "00:11:22:33:44:55"
```

### 4. Print Multiple Copies
Use the `-c` / `--copies` flag:
```bash
./print_image.py photo.jpg --copies 2
```
