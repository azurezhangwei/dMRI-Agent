"""Invert BRAINSFit's transform using Slicer's bundled SimpleITK."""
import sys
import SimpleITK as sitk

def invert(source, destination):
    transform = sitk.ReadTransform(source)
    sitk.WriteTransform(transform.GetInverse(), destination)

if __name__ == "__main__":
    invert(sys.argv[1], sys.argv[2])
