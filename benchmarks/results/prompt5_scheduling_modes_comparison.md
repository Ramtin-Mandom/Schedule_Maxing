# Scheduling modes comparison

Objectives of different modes are not comparable on one scale; no global optimum is claimed. Normal/ADHD equality with the pre-change engine: benchmarks/scheduling_mode_baseline.py --check.

Environment: Python 3.10.11 on Windows-10-10.0.19045-SP0

## non_round_minutes_and_unschedulable_optional

| mode | median ms | status | scheduled | scheduled_minutes | unscheduled | first_start_minute | last_finish_minute | idle_gap_minutes | fixed_minutes | baseline_reward | mode_component | objective | constraint_violations |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| precise_greedy | 0.985 | generated | 3 | 61 | 1 | 19 | 80 | 0 | 12 | 22.0 | 0.0 | 22.0 | 0 |
| adhd_friendly | 0.903 | generated | 3 | 61 | 1 | 19 | 122 | 42 | 12 | 22.0 | 0.0 | 22.0 | 0 |
| early_finish | 1.278 | generated | 3 | 61 | 1 | 19 | 80 | 0 | 12 | 22.0 | 13.3425 | 35.3425 | 0 |
| night_owl | 1.322 | generated | 3 | 61 | 1 | 339 | 400 | 0 | 12 | 19.0 | 13.7138 | 32.7138 | 0 |
| catch_up | 0.89 | generated | 3 | 61 | 1 | 19 | 80 | 0 | 12 | 22.0 | 0.0 | 22.0 | 0 |

## ordinary

| mode | median ms | status | scheduled | scheduled_minutes | unscheduled | first_start_minute | last_finish_minute | idle_gap_minutes | fixed_minutes | baseline_reward | mode_component | objective | constraint_violations |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| precise_greedy | 4.184 | generated | 4 | 155 | 0 | 60 | 495 | 250 | 30 | 32.06 | 0.0 | 32.06 | 0 |
| adhd_friendly | 3.644 | generated | 4 | 155 | 0 | 60 | 495 | 250 | 30 | 32.06 | 0.0 | 32.06 | 0 |
| early_finish | 5.69 | generated | 4 | 155 | 0 | 0 | 155 | 0 | 30 | 29.41 | 17.994 | 47.404 | 0 |
| night_owl | 5.726 | generated | 4 | 155 | 0 | 685 | 840 | 0 | 30 | 27.19 | 17.8289 | 45.0189 | 0 |
| catch_up | 4.101 | generated | 4 | 155 | 0 | 60 | 495 | 250 | 30 | 32.06 | 0.0 | 32.06 | 0 |

## constrained_breaks_deadlines_dependencies

| mode | median ms | status | scheduled | scheduled_minutes | unscheduled | first_start_minute | last_finish_minute | idle_gap_minutes | fixed_minutes | baseline_reward | mode_component | objective | constraint_violations |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| precise_greedy | 1.185 | generated | 4 | 210 | 0 | 0 | 360 | 30 | 120 | 20.0 | 0.0 | 20.0 | 0 |
| adhd_friendly | 1.172 | generated | 4 | 210 | 0 | 0 | 360 | 30 | 120 | 20.0 | 0.0 | 20.0 | 0 |
| early_finish | 1.981 | generated | 4 | 210 | 0 | 0 | 360 | 30 | 120 | 20.0 | 13.8802 | 33.8802 | 0 |
| night_owl | 2.001 | generated | 4 | 210 | 0 | 405 | 720 | 105 | 120 | 24.0 | 13.9167 | 37.9167 | 0 |
| catch_up | 1.357 | generated | 4 | 210 | 0 | 0 | 360 | 30 | 120 | 20.0 | 2.2059 | 22.2059 | 0 |

## preferences

| mode | median ms | status | scheduled | scheduled_minutes | unscheduled | first_start_minute | last_finish_minute | idle_gap_minutes | fixed_minutes | baseline_reward | mode_component | objective | constraint_violations |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| precise_greedy | 1.322 | generated | 3 | 135 | 0 | 60 | 315 | 120 | 0 | 27.53 | 0.0 | 27.53 | 0 |
| adhd_friendly | 1.272 | generated | 3 | 135 | 0 | 60 | 315 | 120 | 0 | 27.53 | 0.0 | 27.53 | 0 |
| early_finish | 1.694 | generated | 3 | 135 | 0 | 60 | 315 | 120 | 0 | 27.53 | 11.0357 | 38.5657 | 0 |
| night_owl | 1.774 | generated | 3 | 135 | 0 | 60 | 315 | 120 | 0 | 27.53 | 5.4375 | 32.9675 | 0 |
| catch_up | 1.318 | generated | 3 | 135 | 0 | 60 | 315 | 120 | 0 | 27.53 | 0.0 | 27.53 | 0 |

## large_recurring_workload

| mode | median ms | status | scheduled | scheduled_minutes | unscheduled | first_start_minute | last_finish_minute | idle_gap_minutes | fixed_minutes | baseline_reward | mode_component | objective | constraint_violations |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| precise_greedy | 46.259 | generated | 20 | 800 | 0 | 0 | 920 | 60 | 60 | 132.0 | 0.0 | 132.0 | 0 |
| adhd_friendly | 39.633 | generated | 18 | 710 | 2 | 0 | 945 | 175 | 60 | 120.0 | 0.0 | 120.0 | 0 |
| early_finish | 47.477 | generated | 20 | 800 | 0 | 0 | 860 | 0 | 60 | 130.0 | 48.8021 | 178.8021 | 0 |
| night_owl | 47.026 | generated | 20 | 800 | 0 | 85 | 960 | 15 | 60 | 126.0 | 46.9544 | 172.9544 | 0 |
| catch_up | 46.176 | generated | 20 | 800 | 0 | 0 | 860 | 0 | 60 | 132.0 | 16.5686 | 148.5686 | 0 |

## infeasible_required_work

| mode | median ms | status | scheduled | scheduled_minutes | unscheduled | first_start_minute | last_finish_minute | idle_gap_minutes | fixed_minutes | baseline_reward | mode_component | objective | constraint_violations |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| precise_greedy | 0.047 | infeasible (required work cannot fit) | — | — | — | — | — | — | — | — | — | — | — |
| adhd_friendly | 0.043 | infeasible (required work cannot fit) | — | — | — | — | — | — | — | — | — | — | — |
| early_finish | 0.044 | infeasible (required work cannot fit) | — | — | — | — | — | — | — | — | — | — | — |
| night_owl | 0.044 | infeasible (required work cannot fit) | — | — | — | — | — | — | — | — | — | — | — |
| catch_up | 0.043 | infeasible (required work cannot fit) | — | — | — | — | — | — | — | — | — | — | — |

## sparse_history

| mode | median ms | status | scheduled | scheduled_minutes | unscheduled | first_start_minute | last_finish_minute | idle_gap_minutes | fixed_minutes | baseline_reward | mode_component | objective | constraint_violations |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| precise_greedy | 1.683 | generated | 6 | 220 | 0 | 0 | 220 | 0 | 0 | 36.0 | 0.0 | 36.0 | 0 |
| adhd_friendly | 3.14 | generated | 6 | 220 | 0 | 0 | 290 | 70 | 0 | 36.0 | 0.0 | 36.0 | 0 |
| early_finish | 54.81 | generated | 6 | 220 | 0 | 0 | 220 | 0 | 0 | 36.0 | 24.9115 | 60.9115 | 0 |
| night_owl | 55.067 | generated | 6 | 220 | 0 | 500 | 720 | 0 | 0 | 36.0 | 24.6823 | 60.6823 | 0 |
| catch_up | 1.735 | generated | 6 | 220 | 0 | 0 | 220 | 0 | 0 | 36.0 | 0.0 | 36.0 | 0 |

