from dps.hyper import run_experiment
from dps.tf.updater import DummyUpdater
from silot.run import basic_config, alg_configs, env_configs, sqair_mnist_eval_prepare_func

import argparse
parser = argparse.ArgumentParser()
parser.add_argument('--conv', action='store_true')
parser.add_argument('--max-digits', type=int, choices=[6, 12], required=True)
args, _ = parser.parse_known_args()

readme = "Evaluate SQAIR experiment on moving_mnist."

durations = dict(
    long=dict(
        max_hosts=1, ppn=6, cpp=2, gpu_set="0,1", pmem=10000, project="rpp-bengioy",
        wall_time="150mins", cleanup_time="5mins", slack_time="5mins", n_repeats=6,
        copy_locally=True, distributions=None,
    ),
    build=dict(
        ppn=1, cpp=1, gpu_set="0", wall_time="180mins", n_repeats=1, distributions=None,
        config=dict(get_updater=DummyUpdater,)
    ),
)

config = basic_config.copy()
config.update(env_configs['moving_mnist'])
alg_name = 'conv_sqair' if args.conv else 'sqair'
config.update(alg_configs[alg_name])

config.update(
    render_first=True,
    n_train=96,
    n_val=1008,
    do_train=False,
    prepare_func=sqair_mnist_eval_prepare_func,
    robust=False,
)

search_params = {
    (True, 12): dict(disc_step_bias=5, step_success_prob=0.4),
    (True, 6): dict(disc_step_bias=5., step_success_prob=0.516),
    (False, 12): dict(disc_step_bias=5., step_success_prob=0.4),
    (False, 6): dict(disc_step_bias=5., step_success_prob=0.516),
}[(args.conv, args.max_digits)]

config.update(**search_params)

if args.max_digits == 6:
    config.update(
        curriculum=(
            [dict(min_digits=i, max_digits=i, n_objects=6, fixed_presence=False) for i in range(1, 2)]
            + [dict(min_digits=i, max_digits=i, n_objects=i, fixed_presence=True) for i in range(2, 13)]
        ),
        # curriculum=(
        #     [dict(min_digits=i, max_digits=i, n_objects=6, fixed_presence=False) for i in range(1, 7)]
        #     + [dict(min_digits=i, max_digits=i, n_objects=i, fixed_presence=True) for i in range(7, 13)]
        # ),
    )
else:
    config.update(
        curriculum=[dict(min_digits=i, max_digits=i) for i in range(1, 13)],
        n_objects=12,
    )

run_experiment(
    "eval_moving_mnist_sqair_fixed",
    config, "sqair on moving_mnist.",
    name_variables="max_digits",
    durations=durations
)
