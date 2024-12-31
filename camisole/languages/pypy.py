from camisole.models import Lang, Program


class Pypy(Lang, name="pypy"):
    source_ext = '.pypy'
    interpreter = Program('pypy3', opts=[])
    reference_source = r'print("42")'