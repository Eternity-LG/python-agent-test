# This code demonstrates variable naming conventions in Python, including valid and invalid identifiers.
#依赖
import keyword
# Variable names can include letters, digits, and underscores, but cannot start with a digit.
a= 123
b = "Hello, World!"
c = 3.14
d= "ni hao"
_sum = a + c
Student = {"name": "Alice", "age": 20, "grade": "A"}
MAX_VALUE = 100
print(a)
print(b)
print(c)
print(d)
print(_sum)
print(Student)
# 测试 The following function checks if a given string is a valid Python identifier.
def is_valid_identifier(name):
    try:
        exec(f"{name} = None")
        return True
    except:
        return False

print(is_valid_identifier("2var"))  # False
print(is_valid_identifier("var2"))  # True

keywords = keyword.kwlist
print(keywords)  # List of Python keywords that cannot be used as variable names.

#注释使用
# 第一个注释
# 第二个注释
 
'''
第三注释
第四注释
'''
 
"""
第五注释
第六注释
"""

#代码缩进表示代码块，通常使用4个空格或一个制表符进行缩进。
if a > 100:
    print("a is greater than 100")  
else:
    print("a is less than or equal to 100")
    print("This line is not properly indented")  # This will cause an IndentationError.

# \ is used to indicate that a statement continues on the next line.
total = a + \
        c + MAX_VALUE
print("The total is:")
print(total)
str1 = b + \
    d
print(str1)

#数字类型 NUMBER
x = 10
y = 3.14
z = 1 + 2j
t = True
print(x)
print(y)
print(z)
print(t)

#字符串类型 STRING

'''Python 中单引号 ' 和双引号 " 使用完全相同。
使用三引号(或 """)可以指定一个多行字符串。
转义符 \。
例如，\n 表示换行，\t 表示制表符，\\ 表示反斜杠本身。
r 前缀表示原始字符串，忽略转义字符。
例如，r"\n" 将被解释为包含两个字符 '\' 和 'n' 的字符串，而不是换行符。
'''
str2 = "Hello, ""Python!"
print(str2)
str3 = "Hello, " + "Python!"
print(str3)
str4 = "Hello, " * 3
print(str4)
 
str='123456789'
print(str)                 # 输出字符串
print(str[0:-1])           # 输出第一个到倒数第1个的所有字符
print(str[0])              # 输出字符串第一个字符
print(str[2:5])            # 输出从第三个开始到第六个的字符（不包含）
print(str[2:])             # 输出从第三个开始后的所有字符
print(str[1:5:2])          # 输出从第二个开始到第五个且每隔一个的字符（步长为2）
print(str * 2)             # 输出字符串两次
print(str + '你好')         # 连接字符串
 
print('------------------------------')
 
print('hello\nrunoob')      # 使用反斜杠(\)+n转义特殊字符
print(r'hello\nrunoob')     # 在字符串前面添加一个 r，表示原始字符串，不会发生转义


#input("\n\n按下 enter 键后退出。")

#!/usr/bin/python3
 
import sys; x = 'runoob'; sys.stdout.write(x + '\n')

'''使用交互式命令行执行，输出结果为：

>>> import sys; x = 'runoob'; sys.stdout.write(x + '\n')
runoob
7
'''

#!/usr/bin/python3
 
x="a"
y="b"
# 换行输出
print( x )
print( y )
 
print('---------')
# 不换行输出
print( x, end="" )
print( y, end=" " )
print()

#import ,from import ,as
import sys
print('================Python import mode==========================')
print ('命令行参数为:')
_i_length = sys.argv.__len__()
for i in sys.argv:
    print (i)
print ('\n python 路径为',sys.path)
print(_i_length)
print('================Python import mode==========================')

from sys import argv,path  #  导入特定的成员
 
print('================python from import===================================')
print('path:',path) # 因为已经导入path成员，所以此处引用时不需要加sys.path
print('================python from import===================================')

print('--------------------------------------------------------------------------------------')
#基本数据类型
# 变量定义
x = 10          # 整数
y = 3.14         # 浮点数
name = "Alice"   # 字符串
is_active = True # 布尔值

# 多变量赋值
d=e=f=1
a, b, c = 1, 2, "three"

# 查看数据类型
print(type(x))        # <class 'int'>
print(type(y))        # <class 'float'>
print(type(name))     # <class 'str'>
print(type(is_active)) # <class 'bool'>

'''标准数据类型
Python3 中常见的数据类型有：

Number（数字）
String（字符串）
bool（布尔类型）
List（列表）
Tuple（元组）
Set（集合）
Dictionary（字典）
Python3 的六个标准数据类型中：

不可变数据（3 个）：Number（数字）、String（字符串）、Tuple（元组）；
可变数据（3 个）：List（列表）、Dictionary（字典）、Set（集合）。'''