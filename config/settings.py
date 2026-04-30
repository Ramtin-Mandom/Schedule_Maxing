# config/settings.py

# -------------------------
# Time Model
# -------------------------

TIME_SLOT_MINUTES = 30

DEFAULT_DAY_START = 480    # 8:00 AM
DEFAULT_DAY_END = 1320     # 10:00 PM

DEFAULT_MODE = "multi_day"


# -------------------------
# Reward Weights
# -------------------------

WEIGHT_PRIORITY = 5
WEIGHT_PREFERENCE_TIME = 4
WEIGHT_TAG_RELATION = 3
WEIGHT_SPACING = 2
WEIGHT_DEADLINE_BONUS = 3

WEIGHT_NO_BREAK_PENALTY = -2
WEIGHT_LATE_TASK_PENALTY = -5

# Reward behavior constants

PREFERENCE_TIME_DISTANCE_SCALE = 720
TAG_RELATION_MAX_GAP = 120

MIN_GOOD_BREAK = 15
MAX_GOOD_BREAK = 60
BACK_TO_BACK_GAP = 0


# -------------------------
# Hard Constraints
# -------------------------

ALLOW_OVERLAP = False
ALLOW_TASK_SPLITTING = False

MAX_TASKS_PER_DAY = 12

ENFORCE_FIXED_BLOCKS = True
ENFORCE_DAY_WINDOW = True
ENFORCE_DEPENDENCIES = True
ENFORCE_DEADLINES = True


# -------------------------
# Simulated Annealing
# -------------------------

DEFAULT_OPTIMIZER = "simulated_annealing"

INITIAL_TEMPERATURE = 100.0
MIN_TEMPERATURE = 0.1
COOLING_RATE = 0.95

MAX_ITERATIONS = 5000
NO_IMPROVEMENT_LIMIT = 500


# -------------------------
# Neighbor Generation
# -------------------------

ALLOW_MOVE_TASK = True
ALLOW_SWAP_TASKS = True
ALLOW_SHIFT_TASK = True

SHIFT_AMOUNT_MINUTES = 30