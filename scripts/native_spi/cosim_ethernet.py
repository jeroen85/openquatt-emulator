"""W5500 SPI2 transactions using the same MACRAW model and local LAN as native HIL."""
import ctypes

W5500_CS, W5500_CLK, W5500_MOSI, W5500_MISO, W5500_IRQ = 13, 12, 10, 11, 9


class Ethernet:
    def __init__(self, library, port, cable, trace, epoch, drive):
        self.lib = library
        self.drive = drive
        self.irq = None
        self.transfers = 0
        self.lib.oq_cosim_lan_create.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_ulong]
        self.lib.oq_cosim_lan_create.restype = ctypes.c_void_p
        self.lib.oq_cosim_lan_service.argtypes = [ctypes.c_void_p]
        self.lib.oq_cosim_lan_service.restype = ctypes.c_int
        self.lib.oq_cosim_lan_transfer.argtypes = [
            ctypes.c_void_p, ctypes.c_uint16, ctypes.c_uint8, ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t,
        ]
        self.lib.oq_cosim_lan_transfer.restype = ctypes.c_int
        self.lib.oq_cosim_lan_destroy.argtypes = [ctypes.c_void_p]
        self.lib.oq_cosim_lan_destroy.restype = None
        self.device = self.lib.oq_cosim_lan_create(port, str(cable).encode(), str(trace).encode(), epoch)
        if not self.device:
            raise RuntimeError("W5500 kon zijn lokale Ethernetpoort niet openen")

    def update_irq(self, asserted, at):
        if asserted < 0:
            raise RuntimeError("W5500-netwerkmodel heeft een fout gemeld")
        high = not asserted
        if high != self.irq:
            # Schedule ahead of the guest to accommodate loopback transport latency.
            self.drive(at + 20_000_000, W5500_IRQ, high)
            self.irq = high

    def service(self, at):
        self.update_irq(self.lib.oq_cosim_lan_service(self.device), at)

    def transfer(self, address, control, tx, read_size, at):
        is_write = bool(control & 4)
        if (control & 3 or (is_write and (read_size or not tx)) or (not is_write and not read_size)
                or max(len(tx), read_size) > 4096):
            raise ValueError("Ongeldige SPI2-overdracht voor W5500")
        data = (ctypes.c_uint8 * (len(tx) if is_write else read_size))()
        if is_write:
            data[:] = tx
        self.update_irq(self.lib.oq_cosim_lan_transfer(self.device, address, control, data, len(data)), at)
        self.transfers += 1
        return b"" if is_write else bytes(data)

    def close(self):
        self.lib.oq_cosim_lan_destroy(self.device)
