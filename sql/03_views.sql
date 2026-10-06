-- PlantPulse 03: analytics views (OEE, anomaly scores, failure risk)
USE SCHEMA PLANTPULSE.OPS;

-- OEE = Availability x Performance x Quality, per machine per day
CREATE OR REPLACE VIEW OEE_DAILY AS
WITH agg AS (
  SELECT p.MACHINE_ID, m.LINE, p.SHIFT_DATE,
         SUM(p.PLANNED_MIN)                                  AS planned,
         SUM(p.RUN_MIN)                                      AS run,
         SUM(p.TOTAL_COUNT * m.IDEAL_CYCLE_TIME_S / 60.0)    AS ideal_run,
         SUM(p.TOTAL_COUNT)                                  AS total,
         SUM(p.GOOD_COUNT)                                   AS good
  FROM PRODUCTION_LOG p
  JOIN MACHINES m ON m.MACHINE_ID = p.MACHINE_ID
  GROUP BY p.MACHINE_ID, m.LINE, p.SHIFT_DATE
)
SELECT MACHINE_ID, LINE, SHIFT_DATE,
       planned AS PLANNED_MIN, run AS RUN_MIN, ideal_run AS IDEAL_RUN_MIN,
       total AS TOTAL_COUNT, good AS GOOD_COUNT,
       run / planned                                         AS AVAILABILITY,
       LEAST(ideal_run / run, 1)                             AS PERFORMANCE,
       good / total                                          AS QUALITY,
       (run / planned) * LEAST(ideal_run / run, 1) * (good / total) AS OEE
FROM agg;

-- Rolling z-score vs. a 7-day baseline that ends 24h before each reading
CREATE OR REPLACE VIEW SENSOR_SCORES AS
WITH w AS (
  SELECT r.*,
    AVG(VIBRATION)      OVER (PARTITION BY MACHINE_ID ORDER BY TS ROWS BETWEEN 191 PRECEDING AND 24 PRECEDING) AS vib_mean,
    STDDEV(VIBRATION)   OVER (PARTITION BY MACHINE_ID ORDER BY TS ROWS BETWEEN 191 PRECEDING AND 24 PRECEDING) AS vib_std,
    AVG(TEMPERATURE)    OVER (PARTITION BY MACHINE_ID ORDER BY TS ROWS BETWEEN 191 PRECEDING AND 24 PRECEDING) AS tmp_mean,
    STDDEV(TEMPERATURE) OVER (PARTITION BY MACHINE_ID ORDER BY TS ROWS BETWEEN 191 PRECEDING AND 24 PRECEDING) AS tmp_std,
    COUNT(*)            OVER (PARTITION BY MACHINE_ID ORDER BY TS ROWS BETWEEN 191 PRECEDING AND 24 PRECEDING) AS n_base
  FROM SENSOR_READINGS r
)
SELECT MACHINE_ID, TS, TEMPERATURE, VIBRATION, PRESSURE, RPM,
       IFF(n_base >= 48, (VIBRATION - vib_mean) / NULLIF(vib_std, 0), NULL)   AS Z_VIBRATION,
       IFF(n_base >= 48, (TEMPERATURE - tmp_mean) / NULLIF(tmp_std, 0), NULL) AS Z_TEMPERATURE,
       COALESCE(ABS(Z_VIBRATION) > 3 OR ABS(Z_TEMPERATURE) > 3, FALSE)        AS IS_ANOMALY
FROM w;

-- Failure-risk score (0-100) and days until vibration reaches the 6.5 mm/s alarm limit
CREATE OR REPLACE VIEW MACHINE_RISK AS
WITH latest AS (SELECT MAX(TS) AS max_ts FROM SENSOR_SCORES),
last24 AS (
  SELECT MACHINE_ID,
         AVG(Z_VIBRATION)              AS avg_z_vibration,
         AVG(Z_TEMPERATURE)            AS avg_z_temperature,
         AVG(IFF(IS_ANOMALY, 1, 0))    AS anomaly_rate_24h
  FROM SENSOR_SCORES, latest
  WHERE TS > DATEADD(hour, -24, max_ts)
  GROUP BY MACHINE_ID
),
trend AS (
  SELECT MACHINE_ID,
         REGR_SLOPE(VIBRATION, DATEDIFF(second, max_ts, TS) / 86400.0) AS vibration_trend_per_day
  FROM SENSOR_SCORES, latest
  WHERE TS > DATEADD(hour, -72, max_ts)
  GROUP BY MACHINE_ID
),
cur AS (
  SELECT MACHINE_ID, VIBRATION AS current_vibration, TEMPERATURE AS current_temperature
  FROM SENSOR_SCORES
  QUALIFY ROW_NUMBER() OVER (PARTITION BY MACHINE_ID ORDER BY TS DESC) = 1
),
scored AS (
  SELECT m.MACHINE_ID, m.LINE, m.MACHINE_TYPE,
         ROUND(c.current_vibration, 2)      AS CURRENT_VIBRATION,
         ROUND(c.current_temperature, 1)    AS CURRENT_TEMPERATURE,
         ROUND(t.vibration_trend_per_day, 2) AS VIBRATION_TREND_PER_DAY,
         ROUND(l.avg_z_vibration, 2)        AS AVG_Z_VIBRATION,
         ROUND(l.avg_z_temperature, 2)      AS AVG_Z_TEMPERATURE,
         ROUND(l.anomaly_rate_24h, 2)       AS ANOMALY_RATE_24H,
         ROUND(100 * (0.5 * LEAST(GREATEST(l.avg_z_vibration, 0) / 4, 1)
                    + 0.3 * LEAST(GREATEST(l.avg_z_temperature, 0) / 4, 1)
                    + 0.2 * l.anomaly_rate_24h))::INT AS RISK_SCORE,
         ROUND(IFF(t.vibration_trend_per_day > 0.05,
                   LEAST(GREATEST((6.5 - c.current_vibration) / t.vibration_trend_per_day, 0), 30),
                   30), 1)                  AS DAYS_TO_MAINTENANCE
  FROM MACHINES m
  JOIN last24 l USING (MACHINE_ID)
  JOIN trend  t USING (MACHINE_ID)
  JOIN cur    c USING (MACHINE_ID)
)
SELECT *,
       CASE WHEN RISK_SCORE >= 70 THEN 'HIGH' WHEN RISK_SCORE >= 40 THEN 'MEDIUM' ELSE 'LOW' END AS RISK_LEVEL
FROM scored;

SELECT * FROM MACHINE_RISK ORDER BY RISK_SCORE DESC;
