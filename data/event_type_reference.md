# Event Type Reference

This file provides brief background on raw vendor event and alert types included in the assessment assets.

## PulseForge
- `HIGH_VIBRATION` — machine vibration above expected range
- `TEMP_SPIKE` — rapid or sustained increase in temperature
- `SENSOR_HEALTH_DROP` — degraded confidence in sensor stream
- `POWER_FLUCTUATION` — unstable power signal
- `RECOVERY_SIGNAL` — machine readings returning closer to baseline

## ThermexWatch
- `VIB_WARN` — vibration warning from alternate vendor schema
- `TEMP_WARN` — temperature warning
- `TEMP_CRIT` — severe temperature-related alert
- `POWER_DROP` — abnormal power reduction
- `OK` — nominal state signal

### ThermexWatch Level Guidance
- `1` — nominal
- `2` — low attention
- `3` — moderate attention
- `4` — high attention
- `5` — critical attention

## MaintaFlow
- `inspection` — inspection result or observation
- `maintenance_update` — maintenance workflow or service record
- `operator_note` — manual qualitative note from operator or technician
- `calibration` — calibration-related record

## Notes
- Vendor schemas do not align exactly.
- Alert codes, levels, timestamp styles, and qualitative confidence fields are intentionally inconsistent.
- Use this reference only as basic context. Design decisions remain part of the assessment.