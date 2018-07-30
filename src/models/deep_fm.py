from argparse import ArgumentParser
import shutil
import sys
from typing import Dict

import dask.dataframe as dd
import tensorflow as tf

from src.data.ml_100k import DATA_DEFAULT, build_categorical_columns
from src.logger import get_logger
from src.tf_utils import (tf_csv_dataset, layer_summary, get_binary_predictions, get_binary_losses,
                          get_binary_metric_ops, get_train_op)


def model_fn(features: Dict[str, tf.Tensor], labels: tf.Tensor, mode, params: Dict) -> tf.estimator.EstimatorSpec:
    # feature columns
    categorical_columns = params.get("categorical_columns", [])
    numeric_columns = params.get("numeric_columns", [])
    # structure components
    use_linear = params.get("use_linear", True)
    use_mf = params.get("use_mf", True)
    use_dnn = params.get("use_dnn", True)
    # structure params
    embedding_size = params.get("embedding_size", 16)
    hidden_units = params.get("hidden_units", [64, 64, 64])
    activation_fn = params.get("activation", tf.nn.relu)
    dropout = params.get("dropout", 0)
    # training params
    optimizer = params.get("optimizer", "Adam")
    learning_rate = params.get("learning_rate", 0.001)

    # check params
    categorical_dim = len(categorical_columns)
    numeric_dim = len(numeric_columns)
    if (categorical_dim + numeric_dim) == 0:
        raise ValueError("At least 1 feature column of categorical_columns or numeric_columns must be specified.")
    if not (use_linear or use_mf or use_dnn):
        raise ValueError("At least 1 of linear, mf or dnn component must be used.")

    logits = 0
    if use_linear:
        with tf.variable_scope("linear"):
            linear_logit = tf.feature_column.linear_model(features, categorical_columns + numeric_columns)
            # [None, 1]

            with tf.name_scope("linear"):
                layer_summary(linear_logit)
            logits += linear_logit
            # [None, 1]

    if use_mf or use_dnn:
        with tf.variable_scope("input_layer"):
            # categorical input
            categorical_dim = len(categorical_columns)
            if categorical_dim > 0:
                embedding_columns = [tf.feature_column.embedding_column(col, embedding_size)
                                     for col in categorical_columns]
                embedding_inputs = tf.feature_column.input_layer(features, embedding_columns)
                # [None, c_d * embedding_size]
                input_layer = embedding_inputs
                # [None, c_d * embedding_size]

            # numeric input
            numeric_dim = len(numeric_columns)
            if numeric_dim > 0:
                numeric_inputs = tf.expand_dims(tf.feature_column.input_layer(features, numeric_columns), -1)
                # [None, n_d, 1]
                numeric_embeddings = tf.get_variable("numeric_embeddings", [1, numeric_dim, embedding_size])
                # [1, n_d, embedding_size]
                numeric_embedding_inputs = tf.reshape(numeric_embeddings * numeric_inputs,
                                                      [-1, numeric_dim * embedding_size])
                # [None, n_d * embedding_size]
                input_layer = numeric_embedding_inputs
                # [None, n_d * embedding_size]

                if categorical_dim > 0:
                    input_layer = tf.concat([embedding_inputs, numeric_embedding_inputs], 1)
                    # [None, d * embedding_size]

        if use_mf:
            with tf.variable_scope("mf"):
                # reshape flat embedding input layer to matrix
                embedding_mat = tf.reshape(input_layer, [-1, categorical_dim + numeric_dim, embedding_size])
                # [None, d, embedding_size]
                sum_square = tf.square(tf.reduce_sum(embedding_mat, 1))
                # [None, embedding_size]
                square_sum = tf.reduce_sum(tf.square(embedding_mat), 1)
                # [None, embedding_size]

                with tf.name_scope("logits"):
                    mf_logit = 0.5 * tf.reduce_sum(sum_square - square_sum, 1, keepdims=True)
                    # [None, 1]
                    layer_summary(mf_logit)
                logits += mf_logit
                # [None, 1]

        if use_dnn:
            with tf.variable_scope("dnn/dnn"):
                net = input_layer
                # [None, d * embedding_size]

                for i, hidden_size in enumerate(hidden_units):
                    with tf.variable_scope("hiddenlayer_%s" % i):
                        net = tf.layers.dense(net, hidden_size, activation=activation_fn)
                        # [None, hidden_size]
                        if dropout > 0 and mode == tf.estimator.ModeKeys.TRAIN:
                            net = tf.layers.dropout(net, rate=dropout, training=True)
                            # [None, hidden_size]
                        layer_summary(net)

                with tf.variable_scope('logits'):
                    dnn_logit = tf.layers.dense(net, 1)
                    # [None, 1]
                    layer_summary(dnn_logit)
                logits += dnn_logit
                # [None, 1]

    with tf.name_scope("deep_fm/logits"):
        layer_summary(logits)

    # prediction
    predictions = get_binary_predictions(logits)
    if mode == tf.estimator.ModeKeys.PREDICT:
        return tf.estimator.EstimatorSpec(mode=mode, predictions=predictions)

    # evaluation
    losses = get_binary_losses(labels, predictions)
    metric_ops = get_binary_metric_ops(labels, predictions, losses)
    if mode == tf.estimator.ModeKeys.EVAL:
        return tf.estimator.EstimatorSpec(mode=mode, loss=losses["loss"], eval_metric_ops=metric_ops)

    # training
    train_op = get_train_op(losses["loss"], optimizer, learning_rate)
    tf.summary.scalar("average_loss", losses["average_loss"])
    if mode == tf.estimator.ModeKeys.TRAIN:
        return tf.estimator.EstimatorSpec(mode=mode, loss=losses["loss"], train_op=train_op)


def train_main(args):
    # define feature columns
    df = dd.read_csv(args.train_csv, dtype=DATA_DEFAULT["dtype"]).persist()
    categorical_columns = build_categorical_columns(df, feature_names=DATA_DEFAULT["feature_names"])

    # clean up model directory
    shutil.rmtree(args.model_dir, ignore_errors=True)
    # define model
    model = tf.estimator.Estimator(
        model_fn,
        args.model_dir,
        params={
            "categorical_columns": categorical_columns,
            "use_linear": not args.exclude_linear,
            "use_mf": not args.exclude_mf,
            "use_dnn": not args.exclude_dnn,
            "embedding_size": args.embedding_size,
            "hidden_units": args.hidden_units,
            "dropout": args.dropout,
        }
    )

    logger.debug("model training started.")
    for n in range(args.num_epochs):
        # train model
        model.train(
            input_fn=lambda: tf_csv_dataset(args.train_csv, DATA_DEFAULT["label"],
                                            shuffle=True, batch_size=args.batch_size)
        )
        # evaluate model
        results = model.evaluate(
            input_fn=lambda: tf_csv_dataset(args.test_csv, DATA_DEFAULT["label"],
                                            batch_size=args.batch_size)
        )
        logger.info("epoch %s: %s.", n, results)


if __name__ == '__main__':
    parser = ArgumentParser()
    parser.add_argument("--train-csv", default=DATA_DEFAULT["train_csv"],
                        help="path to the training csv data (default: %(default)s)")
    parser.add_argument("--test-csv", default=DATA_DEFAULT["test_csv"],
                        help="path to the test csv data (default: %(default)s)")
    parser.add_argument("--model-dir", default="checkpoints/deep_fm",
                        help="model directory (default: %(default)s)")
    parser.add_argument("--exclude-linear", action="store_true",
                        help="flag to exclude linear component (default: %(default)s)")
    parser.add_argument("--exclude-mf", action="store_true",
                        help="flag to exclude mf component (default: %(default)s)")
    parser.add_argument("--exclude-dnn", action="store_true",
                        help="flag to exclude dnn component (default: %(default)s)")
    parser.add_argument("--embedding-size", type=int, default=16,
                        help="embedding size (default: %(default)s)")
    parser.add_argument("--hidden-units", type=int, nargs='+', default=[64, 64, 64],
                        help="hidden layer specification (default: %(default)s)")
    parser.add_argument("--dropout", type=float, default=0.1,
                        help="dropout rate (default: %(default)s)")
    parser.add_argument("--batch-size", type=int, default=32,
                        help="batch size (default: %(default)s)")
    parser.add_argument("--num-epochs", type=int, default=16,
                        help="number of training epochs (default: %(default)s)")
    parser.add_argument("--log-path", default="main.log",
                        help="path of log file (default: %(default)s)")
    args = parser.parse_args()

    logger = get_logger(__name__, log_path=args.log_path, console=True)
    logger.debug("call: %s.", " ".join(sys.argv))
    logger.debug("ArgumentParser: %s.", args)
    tf.logging.set_verbosity(tf.logging.INFO)

    try:
        train_main(args)
    except Exception as e:
        logger.exception(e)
        raise e
