# SPDX-License-Identifier: MIT
# Copyright (C) 2026 OpenCCU-Loom authors.

"""Generated str-enums mirroring pkg/hmenum from the openccu-loom daemon.

DO NOT EDIT BY HAND — run `make generate-enums` after the daemon repo's
`make export-schemas` has refreshed assets/schemas/enums.json.

Each enum value matches the exact wire string the CCU emits. Compare
with `==` against the wire token; the enum members carry the same
Pythonic CamelCase names as the Go constants for cross-language
grep-ability.
"""

from __future__ import annotations

from enum import Enum


class AlarmBlockerPolicy(str, Enum):
    Block = "block"
    Ignore = "ignore"
    Warn = "warn"


class AlarmCentralLossPolicy(str, Enum):
    AlarmCentralLossAlert = "alert"
    AlarmCentralLossTrigger = "trigger"


class AlarmJournalClass(str, Enum):
    Arm = "arm"
    Bypass = "bypass"
    Config = "config"
    Disarm = "disarm"
    Fault = "fault"
    Maintenance = "maintenance"
    Silence = "silence"
    Test = "test"
    Trigger = "trigger"


class AlarmMode(str, Enum):
    Custom = "custom"
    Disarmed = "disarmed"
    Full = "full"
    Night = "night"
    Perimeter = "perimeter"
    Vacation = "vacation"


class AlarmOutputClass(str, Enum):
    AcousticSiren = "acoustic_siren"
    AlarmLight = "alarm_light"
    Chirp = "chirp"
    Notification = "notification"
    OpticalSiren = "optical_siren"
    SmokeSounder = "smoke_sounder"
    SwitchedSiren = "switched_siren"
    SysvarMirror = "sysvar_mirror"


class AlarmPostTriggerPolicy(str, Enum):
    AlarmPostTriggerDisarm = "disarm"
    AlarmPostTriggerReturnToArmed = "return_to_armed"


class AlarmSensorType(str, Enum):
    Door = "door"
    Hazard = "hazard"
    Motion = "motion"
    Panic = "panic"
    Tamper = "tamper"
    Window = "window"


class AlarmZoneState(str, Enum):
    Armed = "armed"
    Arming = "arming"
    Disarmed = "disarmed"
    Pending = "pending"
    Triggered = "triggered"


class Backend(str, Enum):
    CCU = "CCU"
    Homegear = "Homegear"
    PyDevCCU = "PyDevCCU"


class CCUType(str, Enum):
    CCU = "CCU"
    OpenCCU = "OpenCCU"
    Unknown = "Unknown"


class CacheInvalidationReason(str, Enum):
    DeviceAdded = "device_added"
    DeviceRemoved = "device_removed"
    DeviceUpdated = "device_updated"
    Manual = "manual"
    Refresh = "refresh"
    Shutdown = "shutdown"
    Startup = "startup"


class CacheType(str, Enum):
    Data = "data"
    Details = "details"
    DeviceDescription = "device_description"
    ParamsetDescription = "paramset_description"
    Visibility = "visibility"


class CalculatedParameter(str, Enum):
    ApparentTemperature = "APPARENT_TEMPERATURE"
    DewPoint = "DEW_POINT"
    DewPointSpread = "DEW_POINT_SPREAD"
    Enthalpy = "ENTHALPY"
    FrostPoint = "FROST_POINT"
    IntrusionAlarm = "INTRUSION_ALARM"
    OperatingVoltageLevel = "OPERATING_VOLTAGE_LEVEL"
    SmokeAlarm = "SMOKE_ALARM"
    VaporConcentration = "VAPOR_CONCENTRATION"
    WindowOpen = "WINDOW_OPEN"


class CallSource(str, Enum):
    HAInit = "ha_init"
    HMInit = "hm_init"
    ManualOrScheduled = "manual_or_scheduled"


class CentralState(str, Enum):
    Degraded = "degraded"
    Failed = "failed"
    Initializing = "initializing"
    Recovering = "recovering"
    Running = "running"
    Starting = "starting"
    Stopped = "stopped"


class CircuitState(str, Enum):
    Closed = "closed"
    HalfOpen = "half_open"
    Open = "open"


class ClientState(str, Enum):
    Connected = "connected"
    Connecting = "connecting"
    Created = "created"
    Disconnected = "disconnected"
    Failed = "failed"
    Initialized = "initialized"
    Initializing = "initializing"
    Reconnecting = "reconnecting"
    Stopped = "stopped"
    Stopping = "stopping"


class ClimateMode(str, Enum):
    Auto = "auto"
    Cool = "cool"
    Heat = "heat"
    Off = "off"


class ClimateProfile(str, Enum):
    Away = "away"
    Boost = "boost"
    Comfort = "comfort"
    Eco = "eco"
    None_ = "none"
    WeekProgram1 = "week_program_1"
    WeekProgram2 = "week_program_2"
    WeekProgram3 = "week_program_3"
    WeekProgram4 = "week_program_4"
    WeekProgram5 = "week_program_5"
    WeekProgram6 = "week_program_6"


class CommandRxMode(str, Enum):
    Burst = "BURST"
    LazyConfig = "LAZY_CONFIG"
    Unset = ""
    Wakeup = "WAKEUP"


class DataFetchOperation(str, Enum):
    FetchDeviceDescriptions = "fetch_device_descriptions"
    FetchParamsetDescriptions = "fetch_paramset_descriptions"


class DataPointCategory(str, Enum):
    Action = "action"
    ActionNumber = "action_number"
    ActionSelect = "action_select"
    AlarmControlPanel = "alarm_control_panel"
    BinarySensor = "binary_sensor"
    Button = "button"
    Climate = "climate"
    Cover = "cover"
    Event = "event"
    EventGroup = "event_group"
    HubBinarySensor = "hub_binary_sensor"
    HubButton = "hub_button"
    HubNumber = "hub_number"
    HubSelect = "hub_select"
    HubSensor = "hub_sensor"
    HubSwitch = "hub_switch"
    HubText = "hub_text"
    HubUpdate = "hub_update"
    Light = "light"
    Lock = "lock"
    Number = "number"
    ScheduleSwitch = "schedule_switch"
    Select = "select"
    Sensor = "sensor"
    Siren = "siren"
    Switch = "switch"
    Text = "text"
    TextDisplay = "text_display"
    Undefined = "undefined"
    Update = "update"
    Valve = "valve"
    WeekProfile = "week_profile"


class DataPointType(str, Enum):
    AlarmControlPanel = "alarm_control_panel"
    BinarySensor = "binary_sensor"
    Button = "button"
    Climate = "climate"
    Cover = "cover"
    Event = "event"
    Light = "light"
    Lock = "lock"
    Number = "number"
    Select = "select"
    Sensor = "sensor"
    Siren = "siren"
    Switch = "switch"
    Text = "text"
    Update = "update"
    Valve = "valve"


class DataPointUsage(str, Enum):
    CDPPrimary = "ce_primary"
    CDPSecondary = "ce_secondary"
    CDPState = "ce_state"
    CDPVisible = "ce_visible"
    DataPoint = "data_point"
    Event = "event"
    Ignored = "ignored"
    NoCreate = "no_create"


class DataRefreshType(str, Enum):
    AlarmMessages = "alarm_messages"
    ClientData = "client_data"
    Connectivity = "connectivity"
    Inbox = "inbox"
    Metrics = "metrics"
    Program = "program"
    ServiceMessages = "service_messages"
    SystemUpdate = "system_update"
    Sysvar = "sysvar"


class DescriptionMarker(str, Enum):
    HAHM = "HAHM"
    HX = "HX"
    Internal = "INTERNAL"
    MQTT = "MQTT"


class DeviceFirmwareState(str, Enum):
    BackgroundUpdateNotSupported = "BACKGROUND_UPDATE_NOT_SUPPORTED"
    DeliverFirmwareImage = "DELIVER_FIRMWARE_IMAGE"
    DoUpdatePending = "DO_UPDATE_PENDING"
    LiveDeliverFirmwareImage = "LIVE_DELIVER_FIRMWARE_IMAGE"
    LiveNewFirmwareAvailable = "LIVE_NEW_FIRMWARE_AVAILABLE"
    LiveUpToDate = "LIVE_UP_TO_DATE"
    NewFirmwareAvailable = "NEW_FIRMWARE_AVAILABLE"
    PerformingUpdate = "PERFORMING_UPDATE"
    ReadyForUpdate = "READY_FOR_UPDATE"
    Unknown = "UNKNOWN"
    UpToDate = "UP_TO_DATE"


class DeviceLifecycleSubtype(str, Enum):
    AvailabilityChanged = "AVAILABILITY_CHANGED"
    Created = "CREATED"
    Delayed = "DELAYED"
    Removed = "REMOVED"
    Updated = "UPDATED"


class DeviceProfile(str, Enum):
    IPAccessPermission = "IPAccessPermission"
    IPButtonLock = "IPButtonLock"
    IPIrrigationValve = "IPIrrigationValve"
    IPLock = "IPLock"
    IPSiren = "IPSiren"
    IPSirenSmoke = "IPSirenSmoke"
    IPSoundPlayer = "IPSoundPlayer"
    IPSoundPlayerLed = "IPSoundPlayerLed"
    IPSwitch = "IPSwitch"
    IPTextDisplay = "IPTextDisplay"
    IPThermostat = "IPThermostat"
    IPThermostatGroup = "IPThermostatGroup"
    RFButtonLock = "RFButtonLock"
    RfLock = "RfLock"
    RfSwitch = "RfSwitch"
    RfThermostat = "RfThermostat"
    RfThermostatGroup = "RfThermostatGroup"
    SimpleRfThermostat = "SimpleRfThermostat"


class DeviceTriggerEventType(str, Enum):
    DeviceError = "homematic.device_error"
    Impulse = "homematic.impulse"
    Keypress = "homematic.keypress"


class DeviceUpdateStatus(str, Enum):
    Installing = "installing"
    UpToDate = "up_to_date"
    UpdateAvailable = "update_available"


class DuressVisibility(str, Enum):
    Full = "full"
    Hidden = "hidden"
    NotifyOnly = "notify_only"


class FailureReason(str, Enum):
    Auth = "auth"
    CircuitBreaker = "circuit_breaker"
    Exhausted = "exhausted"
    Internal = "internal"
    Network = "network"
    None_ = "none"
    Timeout = "timeout"
    Unknown = "unknown"


class Field(str, Enum):
    AcousticAlarmActive = "acoustic_alarm_active"
    AcousticAlarmSelection = "acoustic_alarm_selection"
    AcousticNotificationSelection = "acoustic_notification_selection"
    ActiveProfile = "active_profile"
    ActivityState = "activity_state"
    AutoMode = "auto_mode"
    BoostMode = "boost_mode"
    BurstLimitWarning = "burst_limit_warning"
    ButtonLock = "button_lock"
    ChannelColor = "channel_color"
    Color = "color"
    ColorBehaviour = "color_behaviour"
    ColorLevel = "color_temp"
    ColorTemperature = "color_temperature"
    CombinedParameter = "combined_parameter"
    ComfortMode = "comfort_mode"
    Concentration = "concentration"
    ControlMode = "control_mode"
    Current = "current"
    DeviceOperationMode = "device_operation_mode"
    Direction = "direction"
    DisplayDataAlignment = "display_data_alignment"
    DisplayDataBackgroundColor = "display_data_background_color"
    DisplayDataCommit = "display_data_commit"
    DisplayDataID = "display_data_id"
    DisplayDataIcon = "display_data_icon"
    DisplayDataString = "display_data_string"
    DisplayDataTextColor = "display_data_text_color"
    DoorCommand = "door_command"
    DoorState = "door_state"
    Duration = "duration"
    DurationUnit = "duration_unit"
    DurationValue = "duration_value"
    DutyCycle = "duty_cycle"
    Dutycycle = "dutycycle"
    Effect = "effect"
    EnergyCounter = "energy_counter"
    Error = "error"
    Frequency = "frequency"
    GroupLevel = "group_level"
    GroupLevel2 = "group_level_2"
    GroupState = "group_state"
    HeatingCooling = "heating_cooling"
    HeatingValveType = "heating_valve_type"
    Hue = "hue"
    Humidity = "humidity"
    Inhibit = "inhibit"
    Interval = "interval"
    Level = "level"
    Level2 = "level_2"
    LevelCombined = "level_combined"
    LockState = "lock_state"
    LockTargetLevel = "lock_target_level"
    LowBat = "low_bat"
    LowBatLimit = "low_bat_limit"
    Lowbat = "lowbat"
    LoweringMode = "lowering_mode"
    ManuMode = "manu_mode"
    MinMaxValueNotRelevantForManuMode = "min_max_value_not_relevant_for_manu_mode"
    OnTimeList = "on_time_list_1"
    OnTimeUnit = "on_time_unit"
    OnTimeValue = "on_time_value"
    Open = "open"
    OperatingVoltage = "operating_voltage"
    OperationMode = "channel_operation_mode"
    OpticalAlarmActive = "optical_alarm_active"
    OpticalAlarmSelection = "optical_alarm_selection"
    OptimumStartStop = "optimum_start_stop"
    PartyMode = "party_mode"
    Power = "power"
    Program = "program"
    RampTimeToOffUnit = "ramp_time_to_off_unit"
    RampTimeToOffValue = "ramp_time_to_off_value"
    RampTimeUnit = "ramp_time_unit"
    RampTimeValue = "ramp_time_value"
    Repetitions = "repetitions"
    RssiDevice = "rssi_device"
    RssiPeer = "rssi_peer"
    Sabotage = "sabotage"
    Saturation = "saturation"
    Section = "section"
    SetPointMode = "set_point_mode"
    Setpoint = "setpoint"
    SmokeDetectorAlarmStatus = "smoke_detector_alarm_status"
    SmokeDetectorCommand = "smoke_detector_command"
    Soundfile = "soundfile"
    State = "state"
    Stop = "stop"
    SwitchMain = "switch_main"
    SwitchV1 = "vswitch_1"
    SwitchV2 = "vswitch_2"
    Temperature = "temperature"
    TemperatureMaximum = "temperature_maximum"
    TemperatureMinimum = "temperature_minimum"
    TemperatureOffset = "temperature_offset"
    ValveState = "valve_state"
    Voltage = "voltage"
    WeekProgramPointer = "week_program_pointer"


class ForcedDeviceAvailability(str, Enum):
    ForceFalse = "forced_not_available"
    ForceTrue = "forced_available"
    NotSet = "not_set"


class HubValueType(str, Enum):
    Alarm = "ALARM"
    Float = "FLOAT"
    Integer = "INTEGER"
    List = "LIST"
    Logic = "LOGIC"
    Number = "NUMBER"
    String = "STRING"


class IncidentSeverity(str, Enum):
    Critical = "critical"
    Error = "error"
    Info = "info"
    Warning = "warning"


class IncidentType(str, Enum):
    AuthFailure = "auth_failure"
    CallbackTimeout = "callback_timeout"
    CircuitBreakerOpen = "circuit_breaker_open"
    CircuitBreakerRecovered = "circuit_breaker_recovered"
    CircuitBreakerTripped = "circuit_breaker_tripped"
    ConfigError = "config_error"
    ConnectionLost = "connection_lost"
    DeviceProfileMiss = "device_profile_miss"
    ParamsetInconsistency = "paramset_inconsistency"
    ParamsetPatch = "paramset_patch"
    PingPongMismatch = "pingpong_mismatch"
    PingPongMismatchHigh = "pingpong_mismatch_high"
    PingPongUnknownHigh = "pingpong_unknown_high"
    RPCError = "rpc_error"
    RPCFault = "rpc_fault"
    RecoveryFailed = "recovery_failed"
    RetryExhausted = "retry_exhausted"


class IntegrationIssueSeverity(str, Enum):
    Error = "error"
    Warning = "warning"


class IntegrationIssueType(str, Enum):
    FetchDataFailed = "fetch_data_failed"
    IncompleteDeviceData = "incomplete_device_data"
    ParamsetInconsistent = "paramset_inconsistency"
    PingPongMismatch = "ping_pong_mismatch"


class Interface(str, Enum):
    BidCosRF = "BidCos-RF"
    BidCosWired = "BidCos-Wired"
    CUxD = "CUxD"
    HmIPRF = "HmIP-RF"
    VirtualDevices = "VirtualDevices"


class InternalCustomID(str, Enum):
    Default = "cid_default"
    LinkPeer = "cid_link_peer"
    ManuTemp = "cid_manu_temp"


class Manufacturer(str, Enum):
    EQ3 = "eQ-3"
    HB = "Homebrew"
    Moehlenhoff = "M\u00f6hlenhoff"


class OptionalSettings(str, Enum):
    OptionalSettingSRDisableRandomizeOutput = "SR_DISABLE_RANDOMIZED_OUTPUT"
    OptionalSettingSRRecordSystemInit = "SR_RECORD_SYSTEM_INIT"


class Parameter(str, Enum):
    AccessAuthorization = "ACCESS_AUTHORIZATION"
    AcousticAlarmActive = "ACOUSTIC_ALARM_ACTIVE"
    AcousticAlarmSelection = "ACOUSTIC_ALARM_SELECTION"
    AcousticNotificationSelection = "ACOUSTIC_NOTIFICATION_SELECTION"
    ActiveProfile = "ACTIVE_PROFILE"
    ActivityState = "ACTIVITY_STATE"
    ActualHumidity = "ACTUAL_HUMIDITY"
    ActualTemperature = "ACTUAL_TEMPERATURE"
    AirPressure = "AIR_PRESSURE"
    AlarmState = "ALARMSTATE"
    AutoMode = "AUTO_MODE"
    AutoRelockState = "AUTO_RELOCK_STATE"
    BatteryState = "BATTERY_STATE"
    BlockedPermanent = "BLOCKED_PERMANENT"
    BlockedTemporary = "BLOCKED_TEMPORARY"
    BoostMode = "BOOST_MODE"
    BurstLimitWarning = "BURST_LIMIT_WARNING"
    ButtonLock = "BUTTON_LOCK"
    ChannelColor = "CHANNEL_COLOR"
    ChannelLock = "CHANNEL_LOCK"
    ChannelOperationMode = "CHANNEL_OPERATION_MODE"
    CodeID = "CODE_ID"
    CodeState = "CODE_STATE"
    Color = "COLOR"
    ColorBehaviour = "COLOR_BEHAVIOUR"
    ColorTemperature = "COLOR_TEMPERATURE"
    CombinedParameter = "COMBINED_PARAMETER"
    ComfortMode = "COMFORT_MODE"
    Concentration = "CONCENTRATION"
    ConfigPending = "CONFIG_PENDING"
    ControlMode = "CONTROL_MODE"
    Current = "CURRENT"
    CurrentIllumination = "CURRENT_ILLUMINATION"
    DeviceOperationMode = "DEVICE_OPERATION_MODE"
    Direction = "DIRECTION"
    DirtLevel = "DIRT_LEVEL"
    DisplayDataAlignment = "DISPLAY_DATA_ALIGNMENT"
    DisplayDataBackgroundColor = "DISPLAY_DATA_BACKGROUND_COLOR"
    DisplayDataCommit = "DISPLAY_DATA_COMMIT"
    DisplayDataID = "DISPLAY_DATA_ID"
    DisplayDataIcon = "DISPLAY_DATA_ICON"
    DisplayDataScrolling = "DISPLAY_DATA_SCROLLING"
    DisplayDataString = "DISPLAY_DATA_STRING"
    DisplayDataTextColor = "DISPLAY_DATA_TEXT_COLOR"
    DoorCommand = "DOOR_COMMAND"
    DoorState = "DOOR_STATE"
    DurationUnit = "DURATION_UNIT"
    DurationValue = "DURATION_VALUE"
    DutyCycle = "DUTY_CYCLE"
    Dutycycle = "DUTYCYCLE"
    Effect = "EFFECT"
    EnergyCounter = "ENERGY_COUNTER"
    EnergyCounterFeedIn = "ENERGY_COUNTER_FEED_IN"
    Error = "ERROR"
    ErrorAlarmTest = "ERROR_ALARM_TEST"
    ErrorJammed = "ERROR_JAMMED"
    ErrorSmokeChamber = "ERROR_SMOKE_CHAMBER"
    Frequency = "FREQUENCY"
    GlobalButtonLock = "GLOBAL_BUTTON_LOCK"
    HeatingCooling = "HEATING_COOLING"
    HeatingValveType = "HEATING_VALVE_TYPE"
    Hue = "HUE"
    Humidity = "HUMIDITY"
    Illumination = "ILLUMINATION"
    Inhibit = "INHIBIT"
    InstallTest = "INSTALL_TEST"
    Interval = "INTERVAL"
    LEDStatus = "LED_STATUS"
    Level = "LEVEL"
    Level2 = "LEVEL_2"
    LevelCombined = "LEVEL_COMBINED"
    LevelReal = "LEVEL_REAL"
    LevelSlats = "LEVEL_SLATS"
    LockState = "LOCK_STATE"
    LockStateReason = "LOCK_STATE_REASON"
    LockTargetLevel = "LOCK_TARGET_LEVEL"
    LowBat = "LOW_BAT"
    LowBatLimit = "LOW_BAT_LIMIT"
    Lowbat = "LOWBAT"
    LoweringMode = "LOWERING_MODE"
    ManuMode = "MANU_MODE"
    MassConcentrationPM10_24H = "MASS_CONCENTRATION_PM_10_24H_AVERAGE"
    MassConcentrationPM1_24H = "MASS_CONCENTRATION_PM_1_24H_AVERAGE"
    MassConcentrationPM25_24H = "MASS_CONCENTRATION_PM_2_5_24H_AVERAGE"
    MinMaxNotRelevantForManuMode = "MIN_MAX_VALUE_NOT_RELEVANT_FOR_MANU_MODE"
    MoistureDetected = "MOISTURE_DETECTED"
    Motion = "MOTION"
    MotionDetectionActive = "MOTION_DETECTION_ACTIVE"
    OnTime = "ON_TIME"
    OnTimeList1 = "ON_TIME_LIST_1"
    OnTimeUnit = "ON_TIME_UNIT"
    OnTimeValue = "ON_TIME_VALUE"
    Open = "OPEN"
    OperatingVoltage = "OPERATING_VOLTAGE"
    OpticalAlarmActive = "OPTICAL_ALARM_ACTIVE"
    OpticalAlarmSelection = "OPTICAL_ALARM_SELECTION"
    OptimumStartStop = "OPTIMUM_START_STOP"
    PartyMode = "PARTY_MODE"
    PartyModeSubmit = "PARTY_MODE_SUBMIT"
    PartyStartDay = "PARTY_START_DAY"
    PartyStartTime = "PARTY_START_TIME"
    PartyStopDay = "PARTY_STOP_DAY"
    PartyStopTime = "PARTY_STOP_TIME"
    PartyTemperature = "PARTY_TEMPERATURE"
    PartyTimeEnd = "PARTY_TIME_END"
    PartyTimeStart = "PARTY_TIME_START"
    PermissionState = "PERMISSION_STATE"
    Pong = "PONG"
    Power = "POWER"
    PresenceDetectionState = "PRESENCE_DETECTION_STATE"
    Press = "PRESS"
    PressCont = "PRESS_CONT"
    PressLock = "PRESS_LOCK"
    PressLong = "PRESS_LONG"
    PressLongRelease = "PRESS_LONG_RELEASE"
    PressLongStart = "PRESS_LONG_START"
    PressShort = "PRESS_SHORT"
    PressUnlock = "PRESS_UNLOCK"
    Program = "PROGRAM"
    RSSIDevice = "RSSI_DEVICE"
    RSSIPeer = "RSSI_PEER"
    Raining = "RAINING"
    RampTime = "RAMP_TIME"
    RampTimeToOffUnit = "RAMP_TIME_TO_OFF_UNIT"
    RampTimeToOffValue = "RAMP_TIME_TO_OFF_VALUE"
    RampTimeUnit = "RAMP_TIME_UNIT"
    RampTimeValue = "RAMP_TIME_VALUE"
    Repetitions = "REPETITIONS"
    ResetMotion = "RESET_MOTION"
    ResetPresence = "RESET_PRESENCE"
    Sabotage = "SABOTAGE"
    SabotageAcceleration = "SABOTAGE_ACCELERATION"
    SabotageBattery = "SABOTAGE_BATTERY"
    SabotageMagneticField = "SABOTAGE_MAGNETIC_FIELD"
    SabotageVertical = "SABOTAGE_VERTICAL"
    Saturation = "SATURATION"
    Section = "SECTION"
    Sensor = "SENSOR"
    SensorError = "SENSOR_ERROR"
    SequenceOK = "SEQUENCE_OK"
    SetPointMode = "SET_POINT_MODE"
    SetPointTemperature = "SET_POINT_TEMPERATURE"
    SetTemperature = "SET_TEMPERATURE"
    Setpoint = "SETPOINT"
    SmokeAlarm = "SMOKE_ALARM"
    SmokeDetectorAlarmStatus = "SMOKE_DETECTOR_ALARM_STATUS"
    SmokeDetectorCommand = "SMOKE_DETECTOR_COMMAND"
    SmokeLevel = "SMOKE_LEVEL"
    Soundfile = "SOUNDFILE"
    State = "STATE"
    StatusValue = "STATUS"
    StickyUnreach = "STICKY_UNREACH"
    Stop = "STOP"
    SunshineDuration = "SUNSHINEDURATION"
    Temperature = "TEMPERATURE"
    TemperatureMaximum = "TEMPERATURE_MAXIMUM"
    TemperatureMinimum = "TEMPERATURE_MINIMUM"
    TemperatureOffset = "TEMPERATURE_OFFSET"
    TimeOfOperation = "TIME_OF_OPERATION"
    Unreach = "UNREACH"
    UpdatePending = "UPDATE_PENDING"
    ValveState = "VALVE_STATE"
    Voltage = "VOLTAGE"
    WaterFlow = "WATER_FLOW"
    WaterLevelDetected = "WATERLEVEL_DETECTED"
    WaterVolume = "WATER_VOLUME"
    WaterVolumeSinceOpen = "WATER_VOLUME_SINCE_OPEN"
    WeekProgramChannelLocks = "WEEK_PROGRAM_CHANNEL_LOCKS"
    WeekProgramPointer = "WEEK_PROGRAM_POINTER"
    WeekProgramTargetChannelLock = "WEEK_PROGRAM_TARGET_CHANNEL_LOCK"
    WeekProgramTargetChannelLocks = "WEEK_PROGRAM_TARGET_CHANNEL_LOCKS"
    WindDirection = "WIND_DIRECTION"
    WindDirectionRange = "WIND_DIRECTION_RANGE"
    WindSpeed = "WIND_SPEED"
    Working = "WORKING"


class ParameterStatus(str, Enum):
    Error = "ERROR"
    External = "EXTERNAL"
    Invalid = "INVALID"
    Normal = "NORMAL"
    Overflow = "OVERFLOW"
    Underflow = "UNDERFLOW"
    Unknown = "UNKNOWN"
    Unused = "UNUSED"


class ParameterType(str, Enum):
    Action = "ACTION"
    Bool = "BOOL"
    Dummy = "DUMMY"
    Empty = ""
    Enum = "ENUM"
    Float = "FLOAT"
    Integer = "INTEGER"
    String = "STRING"


class ParamsetKey(str, Enum):
    Calculated = "CALCULATED"
    Combined = "COMBINED"
    Dummy = "DUMMY"
    Link = "LINK"
    Master = "MASTER"
    Service = "SERVICE"
    Values = "VALUES"


class PingPongMismatchType(str, Enum):
    PingPongMismatchPending = "pending"
    PingPongMismatchUnknown = "unknown"


class ProductGroup(str, Enum):
    HM = "BidCos-RF"
    HmIP = "HmIP-RF"
    HmIPW = "HmIP-Wired"
    HmW = "BidCos-Wired"
    Unknown = "unknown"
    Virtual = "VirtualDevices"


class ProfileKey(str, Enum):
    AdditionalDPs = "additional_dps"
    AllowUndefinedGenericDPs = "allow_undefined_generic_dps"
    DefaultDPs = "default_dps"
    DeviceDefinitions = "device_definitions"
    DeviceGroup = "device_group"
    Fields = "fields"
    IncludeDefaultDPs = "include_default_dps"
    PrimaryChannel = "primary_channel"
    SecondaryChannels = "secondary_channels"
    StateChannel = "state_channel"


class ProgramTrigger(str, Enum):
    API = "api"
    Automation = "automation"
    Scheduler = "scheduler"
    User = "user"


class Quantity(str, Enum):
    Battery = "battery"
    Blind = "blind"
    CO2 = "carbon_dioxide"
    Current = "current"
    Distance = "distance"
    Duration = "duration"
    Energy = "energy"
    Enum = "enum"
    Frequency = "frequency"
    Garage = "garage"
    Gas = "gas"
    Heat = "heat"
    Humidity = "humidity"
    Identify = "identify"
    Illuminance = "illuminance"
    Moisture = "moisture"
    Motion = "motion"
    None_ = ""
    Occupancy = "occupancy"
    Opening = "opening"
    Outlet = "outlet"
    PM1 = "pm1"
    PM10 = "pm10"
    PM25 = "pm25"
    Power = "power"
    Precipitation = "precipitation"
    Presence = "presence"
    Pressure = "pressure"
    Problem = "problem"
    Restart = "restart"
    Running = "running"
    Safety = "safety"
    Shade = "shade"
    Shutter = "shutter"
    SignalStrength = "signal_strength"
    Smoke = "smoke"
    Speed = "speed"
    Switch = "switch"
    Tamper = "tamper"
    Temperature = "temperature"
    Update = "update"
    Voltage = "voltage"
    Volume = "volume"
    VolumeFlowRate = "volume_flow_rate"
    WindDirection = "wind_direction"
    WindSpeed = "wind_speed"
    Window = "window"


class RPCServerType(str, Enum):
    BINRPC = "bin_rpc"
    None_ = "none"
    XMLRPC = "xml_rpc"


class RPCType(str, Enum):
    JSONRPC = "jsonrpc"
    XMLRPC = "xmlrpc"


class ReadinessPhase(str, Enum):
    ReadinessLoadingDevices = "loading_devices"
    ReadinessLoadingHub = "loading_hub"
    ReadinessReady = "ready"
    ReadinessUnknown = "unknown"
    ReadinessWaitingForCCU = "waiting_for_ccu"


class RecoveryResult(str, Enum):
    Cancelled = "cancelled"
    Failed = "failed"
    MaxRetries = "max_retries"
    Partial = "partial"
    Success = "success"


class RecoveryStage(str, Enum):
    Cooldown = "cooldown"
    DataLoading = "data_loading"
    Detecting = "detecting"
    Failed = "failed"
    Heartbeat = "heartbeat"
    Idle = "idle"
    RPCChecking = "rpc_checking"
    Reconnecting = "reconnecting"
    Recovered = "recovered"
    StabilityCheck = "stability_check"
    SyncHubData = "sync_hub_data"
    TCPChecking = "tcp_checking"
    WarmingUp = "warming_up"


class RegaScript(str, Enum):
    AcceptDeviceInInbox = "accept_device_in_inbox"
    AcknowledgeAllAlarmMessages = "acknowledge_all_alarm_messages"
    AcknowledgeAllServiceMessages = "acknowledge_all_service_messages"
    AcknowledgeMessage = "acknowledge_message"
    CreateBackupStart = "create_backup_start"
    CreateBackupStatus = "create_backup_status"
    CreateFunction = "create_function"
    CreateRoom = "create_room"
    CreateSystemVariable = "create_system_variable"
    DeleteFunction = "delete_function"
    DeleteProgram = "delete_program"
    DeleteRoom = "delete_room"
    ExecuteProgramConditional = "execute_program_conditional"
    FetchAllDeviceData = "fetch_all_device_data"
    GetAlarmMessages = "get_alarm_messages"
    GetBackendInfo = "get_backend_info"
    GetInboxDevices = "get_inbox_devices"
    GetProgramDescriptions = "get_program_descriptions"
    GetSerial = "get_serial"
    GetServiceMessages = "get_service_messages"
    GetSystemUpdateInfo = "get_system_update_info"
    GetSystemVariableDescriptions = "get_system_variable_descriptions"
    GetUserLevel = "get_user_level"
    PollComTest = "poll_com_test"
    PoweroffCCU = "poweroff_ccu"
    RebootCCU = "reboot_ccu"
    RenameFunction = "rename_function"
    RenameRoom = "rename_room"
    SetCCUPosition = "set_ccu_position"
    SetDeviceFunctions = "set_device_functions"
    SetDeviceRooms = "set_device_rooms"
    SetProgramState = "set_program_state"
    SetSystemVariable = "set_system_variable"
    StartComTest = "start_com_test"
    TriggerFirmwareUpdate = "trigger_firmware_update"
    UpdateSystemVariable = "update_system_variable"
    UsageBySysvar = "usage_by_sysvar"


class RollbackReason(str, Enum):
    SendError = "send_error"
    Timeout = "timeout"
    ValueMismatch = "mismatch"


class ScheduleField(str, Enum):
    AstroOffset = "ASTRO_OFFSET"
    AstroType = "ASTRO_TYPE"
    ColorType = "HUE_SATURATION_COLOR_TEMPERATURE_EFFECT_TYPE"
    ColorValue = "HUE_SATURATION_COLOR_TEMPERATURE_EFFECT_VALUE"
    Condition = "CONDITION"
    DurationBase = "DURATION_BASE"
    DurationFactor = "DURATION_FACTOR"
    FixedHour = "FIXED_HOUR"
    FixedMinute = "FIXED_MINUTE"
    Level = "LEVEL"
    Level2 = "LEVEL_2"
    OutputBehaviour = "OUTPUT_BEHAVIOUR"
    RampTimeBase = "RAMP_TIME_BASE"
    RampTimeFactor = "RAMP_TIME_FACTOR"
    TargetChannels = "TARGET_CHANNELS"
    Weekday = "WEEKDAY"


class ScheduleProfile(str, Enum):
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"
    P4 = "P4"
    P5 = "P5"
    P6 = "P6"


class ScheduleType(str, Enum):
    Climate = "climate"
    Default = "default"


class SecurityClass(str, Enum):
    Battery = "battery"
    CO = "co"
    Gas = "gas"
    Intrusion = "intrusion"
    Panic = "panic"
    Smoke = "smoke"
    Tamper = "tamper"
    Technical = "technical"
    Water = "water"


class SecurityFaultReason(str, Enum):
    Blocked = "blocked"
    CentralLost = "central_lost"
    DeviceError = "device_error"
    DutyCycle = "duty_cycle"
    LowBattery = "low_battery"
    Tamper = "tamper"
    Unreachable = "unreachable"


class SecuritySeverity(str, Enum):
    Alarm = "alarm"
    Critical = "critical"
    Info = "info"
    OK = "ok"
    Warning = "warning"


class SecurityVerb(str, Enum):
    Cleared = "cleared"
    FailedToArm = "failed_to_arm"
    PreAlarm = "pre_alarm"
    Raised = "raised"
    Silenced = "silenced"
    Test = "test"
    Triggered = "triggered"


class ServiceScope(str, Enum):
    External = "external"
    Internal = "internal"


class SourceOfDeviceCreation(str, Enum):
    Cache = "CACHE"
    Init = "INIT"
    Manual = "MANUAL"
    New = "NEW"
    Refresh = "REFRESH"


class SystemEventType(str, Enum):
    DeleteDevices = "deleteDevices"
    DevicesCreated = "devicesCreated"
    DevicesDelayed = "devicesDelayed"
    Error = "error"
    HubRefreshed = "hubDataPointRefreshed"
    ListDevices = "listDevices"
    NewDevices = "newDevices"
    ReAddedDevice = "readdedDevice"
    ReplaceDevice = "replaceDevice"
    UpdateDevice = "updateDevice"


class ValueBehavior(str, Enum):
    Cumulative = "cumulative"
    Instantaneous = "instantaneous"
    Monotonic = "monotonic"
    None_ = ""


class ValueSource(str, Enum):
    Cache = "cache"
    Live = "live"
    Stale = "stale"
    Unobserved = "unobserved"


class WSEnvelopeKind(str, Enum):
    Change = "change"
    Initial = "initial"
    Refresh = "refresh"


class WeekdayStr(str, Enum):
    Friday = "FRIDAY"
    Monday = "MONDAY"
    Saturday = "SATURDAY"
    Sunday = "SUNDAY"
    Thursday = "THURSDAY"
    Tuesday = "TUESDAY"
    Wednesday = "WEDNESDAY"
