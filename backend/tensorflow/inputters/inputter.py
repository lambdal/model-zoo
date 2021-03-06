"""
Copyright 2018 Lambda Labs. All Rights Reserved.
Licensed under
==========================================================================

Define data interfaces for TF backend
"""
from __future__ import print_function
import abc
import six

from backend.tensorflow.augmenters import augmenter_factory


@six.add_metaclass(abc.ABCMeta)
class Inputter(object):
  def __init__(self, config):
    self.config = config
    self.augmenter = augmenter_factory.get_augmenter(self.config["augmenter"])

  """Data interfaces for TF backend
  """
  @abc.abstractmethod
  def get_samples_fn(self, *argv):
    raise NotImplementedError()

  @abc.abstractmethod
  def parse_fn(self, mode, *argv):
    raise NotImplementedError()

  @abc.abstractmethod
  def input_fn(self, mode, *argv):
    raise NotImplementedError()
