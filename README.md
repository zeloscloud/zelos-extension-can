# Zelos Extension for Controller Area Network (CAN)

> Zelos CAN

A Zelos extension that monitors sensor data and streams environmental and power readings in real-time.

## Features

- 📊 **Real-time sensor monitoring** - Environmental (temperature/humidity) and power (voltage/current) data streaming
- ⚙️ **Configurable sample rate** - Adjust collection interval from 1ms to 1s with fine-grained control
- 🎯 **Interactive actions** - Update settings on the fly
- 🛡️ **Production ready** - Robust error handling and graceful shutdown

## Quick Start

1. **Install** the extension from the Zelos App
2. **Configure** your sensor name and sample interval
3. **Start** the extension to begin streaming data
4. **View** real-time sensor data in your Zelos dashboard

## Configuration

| Setting | Type | Description | Range | Default |
|---------|------|-------------|-------|---------|
| **Sensor Name** | String | Unique identifier for this sensor | 3-50 chars | `sensor-01` |
| **Interval** | Number | Sample interval in seconds | 0.001 - 1.0 (1ms steps) | `0.1` |

All configuration is managed through the Zelos App settings interface.

## Actions

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

## Data Format

The extension streams two event types:

### Environmental Event
```json
{
  "temperature": 22.5,
  "humidity": 55.0
}
```

### Power Event
```json
{
  "voltage": 12.0,
  "current": 2.5
}
```

All values include proper units (°C, %, V, A) and use Float32 types for optimal performance.

## Development

Want to contribute or modify this extension? See [CONTRIBUTING.md](CONTRIBUTING.md) for the complete developer guide.

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
