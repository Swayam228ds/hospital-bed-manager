# Hospital Bed Manager: admission, waiting list and bed planning for crowded hospitals

A web dashboard for government hospitals where patients wait days or weeks for a bed. It answers three questions every day:

1. **Will this patient need a bed?** A model trained on 560,486 real emergency visits predicts admission from what a triage nurse knows at the door.
2. **Who gets the next free bed?** A transparent priority score ranks the waiting list by urgency, days waited and admission probability.
3. **When will beds free up?** A length-of-stay model predicts each admitted patient's discharge date, so the hospital can call people off the waiting list early.

> **Decision support only.** The platform suggests; doctors decide who is admitted.

Data Science Programming course project.

| Team member | Register no. |
|---|---|
| Abhinay Kharb | 24MID0185 |
| Swayam Mohanty | 24MID0088 |
| Omi Menass | 24MID0236 |

---

## Status

All three models are trained, connected to the dashboard and tested.

| Part | Data | Result on held-out data |
|---|---|---|
| Triage admission (M2) | Yale ED triage dataset, 560,486 visits | AUROC 0.880 vs 0.765 for ESI level alone; 0.853 on an ED never seen in training |
| Length of stay (M3) | Hero DMC Heart Institute, Ludhiana, 15,757 admissions | Average error 2.69 days vs 3.10 for the median stay by admission type; daily discharge count error 6.2 vs 7.8 patients/day |
| Admissions forecast (M1) | Hero DMC, plus Ludhiana daily weather and air quality | 5.3 admissions/day average error vs 6.0 for the 7-day average |
| Waiting list priority | Rule, weights editable in the dashboard | |

## Run it

You need **Python 3.10 or newer**. Everything runs on a laptop CPU; no GPU is needed.

```bash
pip install -r requirements.txt
python app.py
```

Open **http://localhost:5000**. The first run creates a demo hospital in `data/hospital.db`: three wards with 60 beds, about 50 admitted patients and 40 people on the waiting list. Each demo patient is a real Ludhiana admission from the test months (so the stay the simulator plays out is the real one), with a made-up name. Emergency patients also get made-up triage observations, since the Ludhiana data has none. Use **Next day** to move the clock forward (patients are discharged, new ones arrive and the top of the queue is admitted) and **Reset demo** to start again.

### Dashboard pages

| Page | What it shows |
|---|---|
| Overview | Beds occupied, waiting list size, discharges and admissions expected tomorrow, and tomorrow's bed gap. A 7-day chart of beds freeing up against expected admissions. |
| Waiting list | The queue ranked by priority. Change urgency, change the priority weights, admit the top patient, add a planned patient. |
| Emergency intake | Triage form that predicts admission probability with the main reasons, then adds the patient to the queue. |
| Bed board | Every bed by ward: free, freeing within a day, or occupied, with each patient's predicted discharge date and range. |
| Model report | Each model's results against its baseline. |

### Your own hospital

**http://localhost:5000/hospital/** is a second, separate platform that starts empty, for a real hospital to set up by hand. It is stored in `data/my_hospital.db`, so the demo can be reset without touching it.

1. **Setup** tab: enter the hospital's name and its typical new admissions per day (used by the forecast until 14 days of its own records exist), then add each ward with its number of beds. Beds can be added or removed later.
2. **Bed board**: click a free bed to record a patient who is already admitted (with their admission date), admit someone from the waiting list, or remove the bed.
3. **Waiting list** and **Emergency intake** work as in the demo.

It runs on the real calendar date, so there is no *Next day* or *Reset demo*. *Erase this hospital* on the Setup tab wipes it and starts again.

## Train the models

The trained triage model is already in `models/`, so the app works without retraining.

**M2, triage admission (Yale).** Download `Results/5v_cleandf.RData` from [yaleemmlc/admissionprediction](https://github.com/yaleemmlc/admissionprediction). It is too big for Python's `pyreadr` (it needs more than 14 GB of RAM), so convert it with R first, then train:

```bash
Rscript training/prepare_yale.R 5v_cleandf.RData data/raw/yale_triage_subset.csv.gz
python training/m2_triage.py
```

This takes about two minutes on a 4-core CPU. It uses only triage-time inputs (ESI level, age, sex, arrival mode and time, the 7 triage vitals, the 60 most common chief complaints and counts of past visits). Race, religion, insurance and other social columns are left out on purpose.

| Triage model results (test set, 112,098 visits) | AUROC | AUPRC |
|---|---|---|
| LightGBM, our model | 0.880 | 0.754 |
| Logistic regression, same inputs | 0.863 | 0.716 |
| ESI level alone (what triage uses today) | 0.765 | 0.496 |
| LightGBM trained on EDs A and B, tested on ED C | 0.853 | |

**M3 and M1 (Ludhiana).** Put the four Hero DMC CSV files (`HDHI Admission data.csv`, `HDHI Pollution Data.csv` and the others) in `data/raw/hdhi/`, then:

```bash
python training/m3_los.py        # length of stay, a few seconds
python training/m1_admissions.py # daily admissions forecast, a few seconds
```

Both scripts split by time: they train on Apr 2017 to Nov 2018 and test on Dec 2018 to Mar 2019, the way a hospital would use them. The admission dates in the file are written two ways (M/D/YYYY and DD/MM/YYYY), and `training/hdhi.py` uses the `month year` column to read each one correctly.

The length-of-stay model uses only what is known on the day of admission: age, sex, rural or urban, emergency or planned, history (diabetes, hypertension, heart disease and so on), admission blood tests, and the admitting diagnosis. Discharge date, outcome and ICU days are left out, because they are only known later. It predicts two numbers: the likely stay (P50) and a stay that 9 in 10 similar patients finish within (P90).

| Length of stay (3,169 test admissions) | Our model | Median stay by admission type |
|---|---|---|
| Average error | 2.69 days | 3.10 days |
| Predicted within 2 days | 56% | 57% |
| Error in patients discharged per day (actual ~26/day) | 6.2 | 7.8 |
| Stays finished by the P90 date | 90% | |

Single stays are hard to predict exactly, but the error in how many beds free up each day, which is what bed planning needs, falls by about a fifth.

| Admissions forecast (121 test days, ~22 admissions/day) | Average error per day |
|---|---|
| Our LightGBM model (day of week, holidays, recent admissions, weather, air quality) | 5.3 |
| Average of the last 7 days | 6.0 |
| Same day last week | 6.6 |

Mondays are the busiest day (22% above average) and Sundays the quietest (28% below). The dashboard uses this weekly and monthly pattern, scaled to the demo hospital's own admissions, because the demo hospital's history is simulated.

## API

| Method | Endpoint | Does |
|---|---|---|
| GET | `/api/overview` | Bed counts, tomorrow's discharges, admissions and bed gap, 7-day forecast |
| GET, POST | `/api/queue` | List the waiting list; add a patient |
| POST | `/api/queue/<id>/admit` | Put the patient in a free bed and predict their stay |
| POST | `/api/queue/<id>/urgency` | Set urgency 1 to 3 |
| POST | `/api/settings` | Change the priority weights |
| POST | `/api/triage` | Admission probability and reasons for one emergency patient |
| GET | `/api/beds` | Every bed with status and predicted discharge |
| POST | `/api/beds/<id>/discharge` | Free a bed |
| POST | `/api/simulate/next-day`, `/api/simulate/reset` | Demo clock |
| GET | `/api/metrics` | Results of every trained model |

## Project structure

```
app.py                  Flask routes for the dashboard
hospital/db.py          SQLite tables (wards, beds, patients, settings)
hospital/models.py      Loads trained models, with clearly labelled fallbacks
hospital/services.py    Waiting list priority, admit, discharge, overview, next-day simulation
hospital/seed_demo.py   Demo hospital with made-up patients
templates/index.html    Dashboard (Tailwind CSS + Chart.js)
training/               Training scripts: m2_triage.py, m3_los.py, m1_admissions.py
models/                 Trained models and their metrics
sepsis_app.py           Earlier sepsis early-warning project (python sepsis_app.py)
```

## Limits

- The triage model learned from US emergency departments, and the length-of-stay and forecast models from one Indian heart hospital. A real deployment would retrain on the hospital's own records.
- Hero DMC is a cardiac centre, so stays and admission patterns may differ from a general government hospital.
- Demo patients are simulated; nothing here is live hospital data.

## Earlier work: sepsis early warning

The previous version of this project, a sepsis risk score from bedside vitals, still runs with `python sepsis_app.py` (files `sepsis_app.py`, `evaluate_model.py`, `generate_test_data.py`, `sepsis-model.onnx`, `MODEL_CARD.md`). It uses a model trained by another team and synthetic test data, so it is no longer part of the dashboard.

## References

- Hero DMC Heart Institute, Ludhiana admissions data (Kaggle: "Hospital Admissions Data"). Bollepalli SC, Sahani AK, et al. *An Optimized Machine Learning Model Accurately Predicts In-Hospital Outcomes at Admission to a Cardiac Unit.* Diagnostics 2022;12(2):241.
- Hong WS, Haimovich AD, Taylor RA. *Predicting hospital admission at emergency department triage using machine learning.* PLoS ONE 2018;13(7):e0201016. https://doi.org/10.1371/journal.pone.0201016 (dataset: https://github.com/yaleemmlc/admissionprediction)
- [LightGBM](https://lightgbm.readthedocs.io/) · [Flask](https://flask.palletsprojects.com/) · [scikit-learn](https://scikit-learn.org/) · [Chart.js](https://www.chartjs.org/)
