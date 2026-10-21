"""Names for the Bluetooth SIG company identifiers seen in manufacturer data.

Most devices advertise no name but nearly all carry a company id, so "Apple" or
"Garmin" is often the only readable thing about a device. The full registry has
thousands of entries; this is the handful that covers what shows up in a home,
and anything else is shown as its hex id.
"""

COMPANIES = {
    0x0002: "Intel", 0x0006: "Microsoft", 0x000A: "CSR", 0x000D: "Texas Instruments",
    0x000F: "Broadcom", 0x001D: "Qualcomm", 0x0030: "STMicroelectronics", 0x0046: "MediaTek",
    0x004C: "Apple", 0x0059: "Nordic Semiconductor", 0x005D: "Realtek", 0x0065: "HP",
    0x006B: "Polar", 0x0075: "Samsung", 0x0078: "Nike", 0x0087: "Garmin", 0x009E: "Bose",
    0x00C4: "LG", 0x00D2: "Dialog Semiconductor", 0x00E0: "Google", 0x012D: "Sony",
    0x0131: "Cypress", 0x0157: "Huami (Amazfit)", 0x0171: "Amazon", 0x01DA: "Logitech",
    0x027D: "Huawei", 0x02E1: "Victron Energy", 0x038F: "Xiaomi", 0x0499: "Ruuvi",
}


def name(company_id):
    """'Apple', '0x1234' for an id not in the table, '' for None."""
    if company_id is None:
        return ""
    return COMPANIES.get(company_id, f"0x{company_id:04X}")
