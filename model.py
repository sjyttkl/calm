import code
import tensorflow as tf
from tensorflow.python.ops import variable_scope as vs
from tensorflow.python.ops import rnn_cell


class BaseModel(object):
  """Hold the code that is shared between all model varients."""

  def __init__(self, max_length, vocab_size, batch_size):
    self.x = tf.placeholder(tf.int32, [batch_size, max_length], name='x')
    self.y = tf.placeholder(tf.int64, [batch_size, max_length], name='y')
    self.seq_len = tf.placeholder(tf.int64, [batch_size], name='seq_len')

    self._embedding_dims = 180
    self._word_embeddings = tf.get_variable('word_embeddings',
                                            [vocab_size, self._embedding_dims])

    self._inputs = tf.nn.embedding_lookup(self._word_embeddings, self.x)
    self.base_bias = tf.get_variable('base_bias', [vocab_size])
    
    # make a mask
    lengths_transposed = tf.expand_dims(tf.to_int32(self.seq_len), 1)
    lengths_tiled = tf.tile(lengths_transposed, [1, max_length])
    r = tf.range(0, max_length, 1)
    range_row = tf.expand_dims(r, 0)
    range_tiled = tf.tile(range_row, [batch_size, 1])
    indicator = tf.less(range_tiled, lengths_tiled)
    sz = [batch_size, max_length]
    self._mask = tf.select(indicator, tf.ones(sz), tf.zeros(sz))

    
class BiasModel(BaseModel):
  def __init__(self, max_length, vocab_size, user_size, fancy_bias=True, use_nce_loss=True):

    self.fancy_bias = fancy_bias
    self.batch_size = 100
    super(BiasModel, self).__init__(max_length, vocab_size, self.batch_size)

    self.username = tf.placeholder(
      tf.int64, [self.batch_size], name='username')

    if fancy_bias:
      self._user_embed_dims = 30
      self._user_embeddings = tf.get_variable(
        'user_embeddings', [user_size, self._user_embed_dims])
      self._shared_user_mat = tf.get_variable(
        'shared_user_mat', [self._embedding_dims, self._user_embed_dims])

    hidden_size = 150
    cell = tf.nn.rnn_cell.LSTMCell(hidden_size, state_is_tuple=True)
    outputs, _ = tf.nn.dynamic_rnn(cell, self._inputs, dtype=tf.float32,
                                   sequence_length=self.seq_len)

    linear_map = tf.get_variable('linear_map', [self._embedding_dims, hidden_size])
    self._weights = tf.matmul(self._word_embeddings, linear_map)
    if fancy_bias:
      self._bias_word_embeddings = tf.matmul(self._word_embeddings, self._shared_user_mat)

    if use_nce_loss:
      num_sampled=128

      losses = []
      if fancy_bias:
        for oo, yy, uu in zip(tf.unpack(outputs), tf.unpack(self.y), tf.unpack(self.username)):
          yy = tf.expand_dims(yy, 1)
          uu = tf.expand_dims(uu, 0)

          loss = self.mynce(oo, yy, uu, num_sampled=num_sampled, num_classes=vocab_size)
          losses.append(loss)
      else:
        for oo, yy in zip(tf.unpack(outputs), tf.unpack(self.y)):
          yy = tf.expand_dims(yy, 1)

          # Sample the negative labels.                                                       
          #   sampled shape: [num_sampled] tensor                                             
          #   true_expected_count shape = [batch_size, 1] tensor                              
          #   sampled_expected_count shape = [num_sampled] tensor
          sampled_values = tf.nn.learned_unigram_candidate_sampler(
            true_classes=yy,
            num_true=1,
            num_sampled=num_sampled,
            unique=True,
            range_max=vocab_size)

          loss = tf.nn.nce_loss(self._weights, self.base_bias, oo, yy, num_sampled=num_sampled,
                                num_classes=vocab_size, sampled_values=sampled_values)
          losses.append(loss)
      losses = tf.pack(losses)
    else:
      # used for calculating perplexity
      reshaped_outputs = tf.reshape(outputs, [-1, hidden_size])
      reshaped_logits = tf.matmul(reshaped_outputs,
                                  self._weights, transpose_b=True)
      logits = tf.reshape(reshaped_logits, [self.batch_size, max_length, -1])
      
      if fancy_bias:
        uembeds = tf.nn.embedding_lookup(self._user_embeddings, self.username)
        ubiases = tf.matmul(self._bias_word_embeddings, uembeds, transpose_b=True)
        logits_with_bias = (tf.transpose(logits, [1, 0, 2]) + tf.transpose(ubiases)
                            + self.base_bias)
        logits_with_bias = tf.transpose(logits_with_bias, [1, 0, 2])
      else:
        logits_with_bias = logits + self.base_bias

      losses = tf.nn.sparse_softmax_cross_entropy_with_logits(logits_with_bias,
                                                              tf.squeeze(self.y))
      self.logits = logits_with_bias
      self.losses = losses

    self.cost = tf.reduce_sum(losses * self._mask) / tf.reduce_sum(self._mask)

    """
    # next word prediction
    self.wordid = tf.placeholder(tf.int32, [1], name='wordid')
    self.prevstate_c = tf.placeholder(tf.float32, [1, cell.state_size.c],
                                    name='prevstate')
    self.prevstate_h = tf.placeholder(tf.float32, [1, cell.state_size.h],
                                      name='prevstate2')
    word_embedding = tf.nn.embedding_lookup(self._word_embeddings,
                                            self.wordid)
    code.interact(local=locals())
    _o, hidden_out = cell(word_embedding, (self.prevstate_c, self.prevstate_h),
                          scope='LSTMCell')
    self.nextstate_h = hidden_out.h
    self.nextstate_c = hidden_out.c
    q = tf.matmul(tf.matmul(_o, linear_map), self._word_embeddings, 
                  transpose_b=True)
    next_word_logit = q + self.base_bias
    self.next_word_prob = tf.nn.softmax(next_word_logit)
    """

  def mynce(self, inputs, labels, uu, num_sampled,
            num_classes, num_true=1,
            remove_accidental_hits=False,
            partition_strategy='mod',
            name='nce_loss'):

      logits, labels = self._compute_sampled_logits(
        inputs, labels, uu, num_sampled, num_classes,
        num_true=num_true,
        subtract_log_q=True,
        remove_accidental_hits=remove_accidental_hits,
        partition_strategy=partition_strategy,
        name=name)
      sampled_losses = tf.nn.sigmoid_cross_entropy_with_logits(
        logits, labels, name="sampled_losses")
      
      return _sum_rows(sampled_losses)


  def _compute_sampled_logits(self, inputs, labels, uu, num_sampled,
                              num_classes, num_true=1,
                              subtract_log_q=True,
                              remove_accidental_hits=False,
                              partition_strategy="mod",
                              name=None):

    weights = self._weights
    if not isinstance(weights, list):
      weights = [weights]

    with tf.name_scope(name, 'compute_sampled_logits', weights + [inputs, labels]):
      if labels.dtype != tf.int64:
        labels = tf.cast(labels, tf.int64)
      labels_flat = tf.reshape(labels, [-1])

      # Sample the negative labels.                                                       
      #   sampled shape: [num_sampled] tensor                                             
      #   true_expected_count shape = [batch_size, 1] tensor                              
      #   sampled_expected_count shape = [num_sampled] tensor
      sampled_values = tf.nn.learned_unigram_candidate_sampler(
        true_classes=labels,
        num_true=1,
        num_sampled=num_sampled,
        unique=True,
        range_max=num_classes)

      sampled, true_expected_count, sampled_expected_count = sampled_values

      # labels_flat is a [batch_size * num_true] tensor                                   
      # sampled is a [num_sampled] int tensor                                             
      all_ids = tf.concat(0, [labels_flat, sampled])

      # weights shape is [num_classes, dim]                                               
      all_w = tf.nn.embedding_lookup(
        weights, all_ids, partition_strategy=partition_strategy)

      code.interact(local=locals())
      # compute the per user biases
      uembeds = tf.nn.embedding_lookup(self._user_embeddings, uu)
      biases = tf.matmul(tf.nn.embedding_lookup(self._bias_word_embeddings, all_ids), uembeds,
                         transpose_b=True)
      all_b = tf.squeeze(biases) + tf.nn.embedding_lookup(self.base_bias, all_ids)

      # true_w shape is [batch_size * num_true, dim]                                      
      # true_b is a [batch_size * num_true] tensor                                        
      true_w = tf.slice(
        all_w, [0, 0], tf.pack([tf.shape(labels_flat)[0], -1]))
      true_b = tf.slice(all_b, [0], tf.shape(labels_flat))
    
      # inputs shape is [batch_size, dim]                                                 
      # true_w shape is [batch_size * num_true, dim]                                      
      # row_wise_dots is [batch_size, num_true, dim]                                      
      dim = tf.shape(true_w)[1:2]
      new_true_w_shape = tf.concat(0, [[-1, num_true], dim])
      row_wise_dots = tf.mul(
        tf.expand_dims(inputs, 1),
        tf.reshape(true_w, new_true_w_shape))
      # We want the row-wise dot plus biases which yields a                               
      # [batch_size, num_true] tensor of true_logits.                                     
      dots_as_matrix = tf.reshape(row_wise_dots,
                                  tf.concat(0, [[-1], dim]))
      true_logits = tf.reshape(_sum_rows(dots_as_matrix), [-1, num_true])
      true_b = tf.reshape(true_b, [-1, num_true])
      true_logits += true_b
      
      # Lookup weights and biases for sampled labels.                                     
      #   sampled_w shape is [num_sampled, dim]                                           
      #   sampled_b is a [num_sampled] float tensor                                       
      sampled_w = tf.slice(
        all_w, tf.pack([tf.shape(labels_flat)[0], 0]), [-1, -1])
      sampled_b = tf.slice(all_b, tf.shape(labels_flat), [-1])

      # inputs has shape [batch_size, dim]                                                
      # sampled_w has shape [num_sampled, dim]                                            
      # sampled_b has shape [num_sampled]                                                 
      # Apply X*W'+B, which yields [batch_size, num_sampled]                              
      sampled_logits = tf.matmul(inputs,
                                 sampled_w,
                                 transpose_b=True) + sampled_b
      
      if remove_accidental_hits:
        acc_hits = tf.nn.compute_accidental_hits(
          labels, sampled, num_true=num_true)
        acc_indices, acc_ids, acc_weights = acc_hits

        # This is how SparseToDense expects the indices.                                  
        acc_indices_2d = tf.reshape(acc_indices, [-1, 1])
        acc_ids_2d_int32 = tf.reshape(tf.cast(
          acc_ids, dtypes.int32), [-1, 1])
        sparse_indices = tf.concat(
          1, [acc_indices_2d, acc_ids_2d_int32], "sparse_indices")
        # Create sampled_logits_shape = [batch_size, num_sampled]                         
        sampled_logits_shape = tf.concat(
          0,
          [tf.shape(labels)[:1], tf.expand_dims(num_sampled, 0)])
        if sampled_logits.dtype != acc_weights.dtype:
          acc_weights = tf.cast(acc_weights, sampled_logits.dtype)
          sampled_logits += tf.sparse_to_dense(
            sparse_indices, sampled_logits_shape, acc_weights,
            default_value=0.0, validate_indices=False)

      if subtract_log_q:
        # Subtract log of Q(l), prior probability that l appears in sampled.              
        true_logits -= tf.log(true_expected_count)
        sampled_logits -= tf.log(sampled_expected_count)

      # Construct output logits and labels. The true labels/logits start at col 0.        
      out_logits = tf.concat(1, [true_logits, sampled_logits])
      # true_logits is a float tensor, ones_like(true_logits) is a float tensor           
      # of ones. We then divide by num_true to ensure the per-example labels sum          
      # to 1.0, i.e. form a proper probability distribution.                              
      out_labels = tf.concat(
        1, [tf.ones_like(true_logits) / num_true,
            tf.zeros_like(sampled_logits)])

    return out_logits, out_labels

def _sum_rows(x):
  """Returns a vector summing up each row of the matrix x."""
  # _sum_rows(x) is equivalent to math_ops.reduce_sum(x, 1) when x is                   
  # a matrix.  The gradient of _sum_rows(x) is more efficient than                      
  # reduce_sum(x, 1)'s gradient in today's implementation. Therefore,                   
  # we use _sum_rows(x) in the nce_loss() computation since the loss                    
  # is mostly used for training.                                                        
  cols = tf.shape(x)[1]
  ones_shape = tf.pack([cols, 1])
  ones = tf.ones(ones_shape, x.dtype)
  return tf.reshape(tf.matmul(x, ones), [-1])


