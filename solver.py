import logging
import time
import numpy as np
import tensorflow as tf

DELTA_CLIP = 50.0


class BSDESolver(object):
    """Generic neural net based solver model."""
    def __init__(self, config, bsde, model):
        self.eqn_config = config.eqn_config
        self.net_config = config.net_config
        self.bsde = bsde

        self.model = model(config, bsde)
        self.y_init = self.model.y_init
        lr_schedule = tf.keras.optimizers.schedules.PiecewiseConstantDecay(
            self.net_config.lr_boundaries, self.net_config.lr_values)
        self.optimizer = tf.keras.optimizers.Adam(learning_rate=lr_schedule, epsilon=1e-8)

    def train(self):
        start_time = time.time()
        training_history = []
        valid_data = self.bsde.sample(self.net_config.valid_size)

        # begin sgd iteration
        for step in range(self.net_config.num_iterations+1):
            if step % self.net_config.logging_frequency == 0:
                loss = self.loss_fn(valid_data, training=False).numpy()
                y_init = self.y_init.numpy()[0]
                elapsed_time = time.time() - start_time
                training_history.append([step, loss, y_init, elapsed_time])
                if self.net_config.verbose:
                    logging.info("step: %5u,    loss: %.4e, Y0: %.4e,   elapsed time: %3u" % (
                        step, loss, y_init, elapsed_time))
            self.train_step(self.bsde.sample(self.net_config.batch_size))
        return np.array(training_history)

    def loss_fn(self, inputs, training):
        dw, x = inputs
        y_terminal = self.model(inputs, training)
        delta = y_terminal - self.bsde.g_tf(self.bsde.total_time, x[:, :, -1])
        # use linear approximation outside the clipped range
        loss = tf.reduce_mean(tf.where(tf.abs(delta) < DELTA_CLIP, tf.square(delta),
                                       2 * DELTA_CLIP * tf.abs(delta) - DELTA_CLIP ** 2))

        return loss

    def grad(self, inputs, training):
        with tf.GradientTape(persistent=True) as tape:
            loss = self.loss_fn(inputs, training)
        grad = tape.gradient(loss, self.model.trainable_variables)
        del tape
        return grad

    @tf.function
    def train_step(self, train_data):
        grad = self.grad(train_data, training=True)
        self.optimizer.apply_gradients(zip(grad, self.model.trainable_variables))


class NonsharedModel(tf.keras.Model):
    """Multiple neural nets f_t(x)"""
    def __init__(self, config, bsde):
        super(NonsharedModel, self).__init__()
        self.eqn_config = config.eqn_config
        self.net_config = config.net_config
        self.bsde = bsde
        self.y_init = tf.Variable(np.random.uniform(low=self.net_config.y_init_range[0],
                                                    high=self.net_config.y_init_range[1],
                                                    size=[1])
                                  )
        self.z_init = tf.Variable(np.random.uniform(low=-.1, high=.1,
                                                    size=[1, self.eqn_config.dim])
                                  )

        self.subnet = [FeedForwardSubNet(config) for _ in range(self.bsde.num_time_interval-1)]

    def call(self, inputs, training):
        dw, x = inputs
        time_stamp = np.arange(0, self.eqn_config.num_time_interval) * self.bsde.delta_t
        all_one_vec = tf.ones(shape=tf.stack([tf.shape(dw)[0], 1]), dtype=self.net_config.dtype)
        y = all_one_vec * self.y_init
        z = tf.matmul(all_one_vec, self.z_init)

        for t in range(0, self.bsde.num_time_interval-1):
            y = y - self.bsde.delta_t * (
                self.bsde.f_tf(time_stamp[t], x[:, :, t], y, z)
            ) + tf.reduce_sum(z * dw[:, :, t], 1, keepdims=True)
            z = self.subnet[t](x[:, :, t + 1], training) / self.bsde.dim
        # terminal time
        y = y - self.bsde.delta_t * self.bsde.f_tf(time_stamp[-1], x[:, :, -2], y, z) + \
            tf.reduce_sum(z * dw[:, :, -1], 1, keepdims=True)

        return y
    

class SharedModel(tf.keras.Model):
    """Multiple neural nets f_t(x) sharing first hidden layer"""
    def __init__(self, config, bsde):
        super().__init__()
        self.eqn_config = config.eqn_config
        self.net_config = config.net_config
        self.bsde = bsde
        self.y_init = tf.Variable(np.random.uniform(low=self.net_config.y_init_range[0],
                                                    high=self.net_config.y_init_range[1],
                                                    size=[1])
                                  )
        self.z_init = tf.Variable(np.random.uniform(low=-.1, high=.1,
                                                    size=[1, self.eqn_config.dim])
                                  )

        self.shared_bn_layer = tf.keras.layers.BatchNormalization(
                momentum=0.99,
                epsilon=1e-6,
                beta_initializer=tf.random_normal_initializer(0.0, stddev=0.1),
                gamma_initializer=tf.random_uniform_initializer(0.1, 0.5)
        )
            
        self.shared_dense_layer = tf.keras.layers.Dense(config.net_config.num_hiddens[0],
                                                        use_bias=False,
                                                        activation=None)
        
        config.net_config.num_hiddens = config.net_config.num_hiddens[1:]
        
        self.subnet = [FeedForwardSubNet(config) for _ in range(self.bsde.num_time_interval-1)]

    def call(self, inputs, training):
        dw, x = inputs
        time_stamp = np.arange(0, self.eqn_config.num_time_interval) * self.bsde.delta_t
        all_one_vec = tf.ones(shape=tf.stack([tf.shape(dw)[0], 1]), dtype=self.net_config.dtype)
        y = all_one_vec * self.y_init
        z = tf.matmul(all_one_vec, self.z_init)

        for t in range(0, self.bsde.num_time_interval-1):
            y = y - self.bsde.delta_t * (
                self.bsde.f_tf(time_stamp[t], x[:, :, t], y, z)
            ) + tf.reduce_sum(z * dw[:, :, t], 1, keepdims=True)
            
            #shared layer
            _x = self.shared_bn_layer(x[:, :, t + 1], training)
            _x = self.shared_dense_layer(_x)
            _x = tf.nn.relu(_x)
            
            #ts-specific layers
            z = self.subnet[t](_x, training) / self.bsde.dim
        # terminal time
        y = y - self.bsde.delta_t * self.bsde.f_tf(time_stamp[-1], x[:, :, -2], y, z) + \
            tf.reduce_sum(z * dw[:, :, -1], 1, keepdims=True)

        return y


class FeedForwardSubNet(tf.keras.Model):
    def __init__(self, config):
        super(FeedForwardSubNet, self).__init__()
        dim = config.eqn_config.dim
        num_hiddens = config.net_config.num_hiddens
        self.bn_layers = [
            tf.keras.layers.BatchNormalization(
                momentum=0.99,
                epsilon=1e-6,
                beta_initializer=tf.random_normal_initializer(0.0, stddev=0.1),
                gamma_initializer=tf.random_uniform_initializer(0.1, 0.5)
            )
            for _ in range(len(num_hiddens) + 2)]
        self.dense_layers = [tf.keras.layers.Dense(num_hiddens[i],
                                                   use_bias=False,
                                                   activation=None)
                             for i in range(len(num_hiddens))]
        # final output should be gradient of size dim
        self.dense_layers.append(tf.keras.layers.Dense(dim, activation=None))

    def call(self, x, training):
        """structure: bn -> (dense -> bn -> relu) * len(num_hiddens) -> dense -> bn"""
        x = self.bn_layers[0](x, training)
        for i in range(len(self.dense_layers) - 1):
            x = self.dense_layers[i](x)
            x = self.bn_layers[i+1](x, training)
            x = tf.nn.relu(x)
        x = self.dense_layers[-1](x)
        x = self.bn_layers[-1](x, training)
        return x
    

class UFModel(tf.keras.Model):
    """Universal function f(x, t)"""
    def __init__(self, config, bsde):
        super().__init__()
        self.eqn_config = config.eqn_config
        self.net_config = config.net_config
        self.bsde = bsde
        self.y_init = tf.Variable(np.random.uniform(low=self.net_config.y_init_range[0],
                                                    high=self.net_config.y_init_range[1],
                                                    size=[1])
                                  )
        self.z_init = tf.Variable(np.random.uniform(low=-.1, high=.1,
                                                    size=[1, self.eqn_config.dim])
                                  )

        self.subnet = FeedForwardSubNet(config)

    def call(self, inputs, training):
        dw, x = inputs
        time_stamp = np.arange(0, self.eqn_config.num_time_interval) * self.bsde.delta_t
        all_one_vec = tf.ones(shape=tf.stack([tf.shape(dw)[0], 1]), dtype=self.net_config.dtype)
        y = all_one_vec * self.y_init
        z = tf.matmul(all_one_vec, self.z_init)

        for t in range(0, self.bsde.num_time_interval-1):
            y = y - self.bsde.delta_t * (
                self.bsde.f_tf(time_stamp[t], x[:, :, t], y, z)
            ) + tf.reduce_sum(z * dw[:, :, t], 1, keepdims=True)
            
            _x = tf.concat([x[:, :, t + 1],
                            t/self.bsde.num_time_interval * tf.ones((x.shape[0], 1), dtype=self.net_config.dtype)
                           ], 1)
            
            z = self.subnet(_x, training) / self.bsde.dim
        # terminal time
        y = y - self.bsde.delta_t * self.bsde.f_tf(time_stamp[-1], x[:, :, -2], y, z) + \
            tf.reduce_sum(z * dw[:, :, -1], 1, keepdims=True)

        return y
    
    
class GRUModel(tf.keras.Model):
    """RNN Model"""
    def __init__(self, config, bsde):
        super().__init__()
        self.eqn_config = config.eqn_config
        self.net_config = config.net_config
        self.bsde = bsde
        self.y_init = tf.Variable(np.random.uniform(low=self.net_config.y_init_range[0],
                                                    high=self.net_config.y_init_range[1],
                                                    size=[1])
                                  )
        self.z_init = tf.Variable(np.random.uniform(low=-.1, high=.1,
                                                    size=[1, self.eqn_config.dim])
                                  )

        self.gru = tf.keras.layers.GRU(config.net_config.num_hiddens[0],
                                       return_sequences=True, 
                                       return_state=True)
        
        self.dense_layers = [tf.keras.layers.Dense(units,
                                                    use_bias=False,
                                                    activation='relu')
                              for units in config.net_config.num_hiddens[1:]]
        
        self.dense_layers.append(tf.keras.layers.Dense(config.eqn_config.dim, 
                                                       activation=None)
                                )
                
    def _call_net(self, inputs, training):
        transposed_inputs = tf.transpose(inputs, [0, 2, 1])
        x, final_state = self.gru(transposed_inputs, training=training)
        for layer in self.dense_layers:
            x = layer(x)
        return x

    def call(self, inputs, training):
        dw, x = inputs
        time_stamp = np.arange(0, self.eqn_config.num_time_interval) * self.bsde.delta_t
        all_one_vec = tf.ones(shape=tf.stack([tf.shape(dw)[0], 1]), dtype=self.net_config.dtype)
        y = all_one_vec * self.y_init
        z = tf.matmul(all_one_vec, self.z_init)
        
        zs = self._call_net(x[:, :, 1:], training)

        for t in range(0, self.bsde.num_time_interval-1):
            y = y - self.bsde.delta_t * (
                self.bsde.f_tf(time_stamp[t], x[:, :, t], y, z)
            ) + tf.reduce_sum(z * dw[:, :, t], 1, keepdims=True)
            
            z = zs[:, t] / self.bsde.dim
            
        # terminal time
        y = y - self.bsde.delta_t * self.bsde.f_tf(time_stamp[-1], x[:, :, -2], y, z) + \
            tf.reduce_sum(z * dw[:, :, -1], 1, keepdims=True)

        return y