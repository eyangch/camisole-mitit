from camisole.models import Lang, Program


class CXX23(Lang, name="C++23"):
    source_ext = '.cc'
    compiler = Program('g++-14', opts=['-std=c++23', '-Wall', '-Wextra', '-O2'])
    reference_source = r'''
#include <iostream>
int main()
{
    std::cout << 42 << std::endl;
    return 0;
}
'''
