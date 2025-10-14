# Zelos Extension Can

> Zelos App extension for CAN

A Zelos extension that monitors sensor data and streams temperature and humidity readings in real-time.

## Features

- 📊 **Real-time sensor monitoring** - Continuous temperature and humidity data streaming
- ⚙️ **Configurable sample rate** - Adjust collection interval from 1kHz to 1Hz (0.001s to 1s)
- 🎯 **Interactive actions** - Query status and update settings on the fly
- 🛡️ **Production ready** - Robust error handling and graceful shutdown

## Quick Start

1. **Install** the extension from the Zelos App marketplace
2. **Configure** your sensor name and sample interval
3. **Start** the extension to begin streaming data
4. **View** real-time sensor data in your Zelos dashboard

## Configuration

| Setting | Type | Description | Range | Default |
|---------|------|-------------|-------|---------|
| **Sensor Name** | String | Unique identifier for this sensor | 3-50 chars | `sensor-01` |
| **Interval** | Number | Sample interval in seconds | 0.001 - 1.0 | `0.1` |

All configuration is managed through the Zelos App settings interface.

## Actions

### Get Status
Returns current monitoring status and uptime.

**Response:**
```json
{
  "state": "RUNNING",
  "running": true,
  "sensor_name": "sensor-01",
  "interval": 0.1,
  "uptime_s": 123.4
}
```

### Set Interval
Updates the sample interval dynamically without restarting.

**Parameter:**
- `seconds` (number, 0.001 to 1.0) - New sample interval

**Response:**
```json
{
  "message": "Interval set to 0.05s",
  "interval": 0.05
}
```

## Data Streams

The extension streams two events:

### Status Event
```json
{
  "state": "RUNNING",
  "uptime_s": 123.4
}
```

### Sensor Event
```json
{
  "temperature": 22.5,
  "humidity": 55.0
}
```

All values include proper units (°C for temperature, % for humidity) and are typed for optimal performance.

## Requirements

- **Zelos** v25.0.20 or higher
- **Python** 3.11+ (managed automatically by Zelos)

## Links

- **Repository**: [github.com/tkeairns/zelos-extension-can](https://github.com/tkeairns/zelos-extension-can)
- **Issues**: [Report bugs or request features](https://github.com/tkeairns/zelos-extension-can/issues)
- **Documentation**: [Zelos Extension Guide](https://docs.zeloscloud.io/extensions)

## Support

For help and support:
- 📖 [Zelos Documentation](https://docs.zeloscloud.io)
- 🐛 [GitHub Issues](https://github.com/tkeairns/zelos-extension-can/issues)
- 📧 taylor@zeloscloud.io

## License

MIT License - see [LICENSE](LICENSE) for details.

---

**Built with [Zelos](https://zeloscloud.io)**
