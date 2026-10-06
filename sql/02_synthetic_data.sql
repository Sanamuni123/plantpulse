-- PlantPulse 02: synthetic but realistic plant data (12 machines, 30 days)
-- Story baked into the data:
--   * M-L1-03 and M-L3-04 are degrading NOW: vibration/temperature ramp over the last 72h
--   * M-L2-04 shows early wear (mild ramp over 5 days) -> MEDIUM risk
--   * M-L2-02 failed 15 days ago, preceded by the same 72h ramp -> proves the signal is predictive
USE SCHEMA PLANTPULSE.OPS;

INSERT OVERWRITE INTO MACHINES
SELECT 'M-' || l.line || '-0' || i.n,
       l.line, l.line_name, l.machine_type,
       DATEADD(day, 150 * (l.li * 4 + i.n), '2019-01-01'::DATE),
       30 + 5 * i.n + 10 * l.li
FROM (SELECT * FROM VALUES (0,'L1','Stamping','Hydraulic Press'),
                           (1,'L2','Welding','Robot Welder'),
                           (2,'L3','Assembly','CNC Mill') AS t(li, line, line_name, machine_type)) l
CROSS JOIN (SELECT * FROM VALUES (1),(2),(3),(4) AS t(n)) i;

-- Degradation factor d: 0 = healthy, 1 = about to fail
CREATE OR REPLACE FUNCTION DEGRADATION(MACHINE_ID VARCHAR, HOURS_AGO FLOAT)
RETURNS FLOAT AS
$$
  CASE
    WHEN MACHINE_ID IN ('M-L1-03', 'M-L3-04') THEN GREATEST(0, LEAST(1, 1 - HOURS_AGO / 72))
    WHEN MACHINE_ID = 'M-L2-02' AND HOURS_AGO BETWEEN 360 AND 432 THEN (432 - HOURS_AGO) / 72
    WHEN MACHINE_ID = 'M-L2-04' THEN 0.25 * GREATEST(0, 1 - HOURS_AGO / 120)
    ELSE 0
  END
$$;

-- Hourly sensor readings: 12 machines x 720 hours
INSERT OVERWRITE INTO SENSOR_READINGS
WITH hours AS (
  SELECT ROW_NUMBER() OVER (ORDER BY SEQ4()) - 1 AS h
  FROM TABLE(GENERATOR(ROWCOUNT => 720))
), base AS (
  SELECT m.MACHINE_ID, h.h,
         DATEADD(hour, -h.h, DATE_TRUNC('hour', CURRENT_TIMESTAMP()))::TIMESTAMP_NTZ AS ts,
         DEGRADATION(m.MACHINE_ID, h.h) AS d
  FROM MACHINES m CROSS JOIN hours h
)
SELECT MACHINE_ID, ts,
       65   + 15  * d + NORMAL(0, 1.5,  RANDOM()),
       2.0  + 4   * d + NORMAL(0, 0.25, RANDOM()),
       6.0  - 1.0 * d + NORMAL(0, 0.15, RANDOM()),
       1500 - 120 * d + NORMAL(0, 20,   RANDOM())
FROM base;

-- 3 shifts x 30 days x 12 machines
INSERT OVERWRITE INTO PRODUCTION_LOG
WITH days AS (
  SELECT ROW_NUMBER() OVER (ORDER BY SEQ4()) - 1 AS day
  FROM TABLE(GENERATOR(ROWCOUNT => 30))
), shifts AS (
  SELECT * FROM VALUES ('A', 0), ('B', 8), ('C', 16) AS t(shift, start_offset)
), base AS (
  SELECT m.MACHINE_ID, m.IDEAL_CYCLE_TIME_S, d.day, s.shift,
         DEGRADATION(m.MACHINE_ID, d.day * 24 + (24 - s.start_offset - 4)) AS deg,
         UNIFORM(10::FLOAT, 45::FLOAT, RANDOM()) + 120 * deg
           + IFF(m.MACHINE_ID = 'M-L2-02' AND d.day = 15 AND s.shift = 'A', 240, 0) AS downtime,
         UNIFORM(0.82::FLOAT, 0.95::FLOAT, RANDOM()) - 0.15 * deg AS perf,
         UNIFORM(0.96::FLOAT, 0.995::FLOAT, RANDOM()) - 0.06 * deg AS qual
  FROM MACHINES m CROSS JOIN days d CROSS JOIN shifts s
)
SELECT MACHINE_ID,
       DATEADD(day, -day, CURRENT_DATE()),
       shift,
       480,
       ROUND(480 - downtime, 1),
       FLOOR((480 - downtime) * 60 / IDEAL_CYCLE_TIME_S * perf),
       FLOOR(FLOOR((480 - downtime) * 60 / IDEAL_CYCLE_TIME_S * perf) * qual)
FROM base;

INSERT OVERWRITE INTO MAINTENANCE_EVENTS
SELECT MACHINE_ID,
       DATEADD(hour, -3, DATEADD(day, -(5 + MOD((ROW_NUMBER() OVER (ORDER BY MACHINE_ID) - 1) * 2, 20)),
               DATE_TRUNC('hour', CURRENT_TIMESTAMP())))::TIMESTAMP_NTZ,
       'PLANNED', 60,
       'Scheduled preventive maintenance: lubrication, belt and filter check.'
FROM MACHINES
UNION ALL
SELECT 'M-L2-02',
       DATEADD(hour, -360, DATE_TRUNC('hour', CURRENT_TIMESTAMP()))::TIMESTAMP_NTZ,
       'FAILURE', 240,
       'Unplanned stop: wire-feed motor bearing seized. Vibration and temperature rose for ~48h before failure. Bearing replaced.';

SELECT 'MACHINES' t, COUNT(*) n FROM MACHINES UNION ALL
SELECT 'SENSOR_READINGS', COUNT(*) FROM SENSOR_READINGS UNION ALL
SELECT 'PRODUCTION_LOG', COUNT(*) FROM PRODUCTION_LOG UNION ALL
SELECT 'MAINTENANCE_EVENTS', COUNT(*) FROM MAINTENANCE_EVENTS;
