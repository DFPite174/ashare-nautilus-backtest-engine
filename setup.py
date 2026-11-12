"""Tag the shipped native bridge as a platform wheel, without rebuilding it."""
from setuptools import Distribution, setup


class BinaryDistribution(Distribution):
    def has_ext_modules(self):
        return True


setup(distclass=BinaryDistribution)
