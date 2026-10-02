# Scheduling modes comparison

Objectives of different modes are not comparable on one scale; no global optimum is claimed. Normal/ADHD equality with the pre-change engine: benchmarks/scheduling_mode_baseline.py --check.

Environment: Python 3.10.11 on Windows-10-10.0.19045-SP0

## non_round_minutes_and_unschedulable_optional

| mode | median ms | status | scheduled | scheduled_minutes | unscheduled | first_start_minute | last_finish_minute | idle_gap_minutes | fixed_minutes | baseline_reward | mode_component | objective | constraint_violations |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| precise_greedy | 0.835 | generated | 3 | 61 | 1 | 19 | 80 | 0 | 12 | 82.0 | 0.0 | 82.0 | 0 |
| adhd_friendly | 0.896 | generated | 3 | 61 | 1 | 19 | 122 | 42 | 12 | 82.0 | 0.0 | 82.0 | 0 |
| early_finish | 1.214 | generated | 3 | 61 | 1 | 19 | 80 | 0 | 12 | 82.0 | 13.3425 | 95.3425 | 0 |
| night_owl | 1.185 | generated | 3 | 61 | 1 | 339 | 400 | 0 | 12 | 79.0 | 13.7138 | 92.7138 | 0 |
| catch_up | 0.804 | generated | 3 | 61 | 1 | 19 | 80 | 0 | 12 | 82.0 | 0.0 | 82.0 | 0 |

## ordinary

| mode | median ms | status | scheduled | scheduled_minutes | unscheduled | first_start_minute | last_finish_minute | idle_gap_minutes | fixed_minutes | baseline_reward | mode_component | objective | constraint_violations |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| precise_greedy | 4.292 | generated | 4 | 155 | 0 | 60 | 495 | 250 | 30 | 112.06 | 0.0 | 112.06 | 0 |
| adhd_friendly | 3.736 | generated | 4 | 155 | 0 | 60 | 495 | 250 | 30 | 112.06 | 0.0 | 112.06 | 0 |
| early_finish | 5.698 | generated | 4 | 155 | 0 | 0 | 155 | 0 | 30 | 109.41 | 17.994 | 127.404 | 0 |
| night_owl | 5.842 | generated | 4 | 155 | 0 | 685 | 840 | 0 | 30 | 107.19 | 17.8289 | 125.0189 | 0 |
| catch_up | 3.961 | generated | 4 | 155 | 0 | 60 | 495 | 250 | 30 | 112.06 | 0.0 | 112.06 | 0 |

## constrained_breaks_deadlines_dependencies

| mode | median ms | status | scheduled | scheduled_minutes | unscheduled | first_start_minute | last_finish_minute | idle_gap_minutes | fixed_minutes | baseline_reward | mode_component | objective | constraint_violations |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| precise_greedy | 1.201 | generated | 4 | 210 | 0 | 0 | 360 | 30 | 120 | 100.0 | 0.0 | 100.0 | 0 |
| adhd_friendly | 1.142 | generated | 4 | 210 | 0 | 0 | 360 | 30 | 120 | 100.0 | 0.0 | 100.0 | 0 |
| early_finish | 1.91 | generated | 4 | 210 | 0 | 0 | 360 | 30 | 120 | 100.0 | 13.8802 | 113.8802 | 0 |
| night_owl | 1.954 | generated | 4 | 210 | 0 | 405 | 720 | 105 | 120 | 104.0 | 13.9167 | 117.9167 | 0 |
| catch_up | 1.308 | generated | 4 | 210 | 0 | 0 | 360 | 30 | 120 | 100.0 | 2.2059 | 102.2059 | 0 |

## preferences

| mode | median ms | status | scheduled | scheduled_minutes | unscheduled | first_start_minute | last_finish_minute | idle_gap_minutes | fixed_minutes | baseline_reward | mode_component | objective | constraint_violations |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| precise_greedy | 1.306 | generated | 3 | 135 | 0 | 60 | 315 | 120 | 0 | 87.53 | 0.0 | 87.53 | 0 |
| adhd_friendly | 1.205 | generated | 3 | 135 | 0 | 60 | 315 | 120 | 0 | 87.53 | 0.0 | 87.53 | 0 |
| early_finish | 1.967 | generated | 3 | 135 | 0 | 60 | 315 | 120 | 0 | 87.53 | 11.0357 | 98.5657 | 0 |
| night_owl | 1.683 | generated | 3 | 135 | 0 | 60 | 315 | 120 | 0 | 87.53 | 5.4375 | 92.9675 | 0 |
| catch_up | 1.453 | generated | 3 | 135 | 0 | 60 | 315 | 120 | 0 | 87.53 | 0.0 | 87.53 | 0 |

## large_recurring_workload

| mode | median ms | status | scheduled | scheduled_minutes | unscheduled | first_start_minute | last_finish_minute | idle_gap_minutes | fixed_minutes | baseline_reward | mode_component | objective | constraint_violations |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| precise_greedy | 41.339 | generated | 20 | 800 | 0 | 0 | 860 | 0 | 60 | 487.0 | 0.0 | 487.0 | 0 |
| adhd_friendly | 39.715 | generated | 15 | 585 | 5 | 0 | 950 | 305 | 60 | 437.0 | 0.0 | 437.0 | 0 |
| early_finish | 42.07 | generated | 20 | 800 | 0 | 0 | 860 | 0 | 60 | 487.0 | 48.8568 | 535.8568 | 0 |
| night_owl | 44.26 | generated | 20 | 800 | 0 | 85 | 960 | 15 | 60 | 483.0 | 46.9154 | 529.9154 | 0 |
| catch_up | 44.213 | generated | 20 | 800 | 0 | 0 | 860 | 0 | 60 | 487.0 | 16.5686 | 503.5686 | 0 |

## infeasible_required_work

| mode | median ms | status | scheduled | scheduled_minutes | unscheduled | first_start_minute | last_finish_minute | idle_gap_minutes | fixed_minutes | baseline_reward | mode_component | objective | constraint_violations |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| precise_greedy | 0.04 | infeasible (required work cannot fit) | — | — | — | — | — | — | — | — | — | — | — |
| adhd_friendly | 0.036 | infeasible (required work cannot fit) | — | — | — | — | — | — | — | — | — | — | — |
| early_finish | 0.042 | infeasible (required work cannot fit) | — | — | — | — | — | — | — | — | — | — | — |
| night_owl | 0.037 | infeasible (required work cannot fit) | — | — | — | — | — | — | — | — | — | — | — |
| catch_up | 0.038 | infeasible (required work cannot fit) | — | — | — | — | — | — | — | — | — | — | — |

## sparse_history

| mode | median ms | status | scheduled | scheduled_minutes | unscheduled | first_start_minute | last_finish_minute | idle_gap_minutes | fixed_minutes | baseline_reward | mode_component | objective | constraint_violations |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| precise_greedy | 1.694 | generated | 6 | 220 | 0 | 0 | 220 | 0 | 0 | 105.0 | 0.0 | 105.0 | 0 |
| adhd_friendly | 3.006 | generated | 6 | 220 | 0 | 0 | 255 | 35 | 0 | 105.0 | 0.0 | 105.0 | 0 |
| early_finish | 54.984 | generated | 6 | 220 | 0 | 0 | 220 | 0 | 0 | 111.0 | 24.9115 | 135.9115 | 0 |
| night_owl | 57.531 | generated | 6 | 220 | 0 | 500 | 720 | 0 | 0 | 111.0 | 24.6823 | 135.6823 | 0 |
| catch_up | 1.741 | generated | 6 | 220 | 0 | 0 | 220 | 0 | 0 | 105.0 | 0.0 | 105.0 | 0 |

